import os
import re
import json
import datetime
import requests
from supabase import create_client, Client
from google import genai
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# --- LOAD SECRETS ---
SUPABASE_URL = os.environ["SUPABASE_URL"].strip().rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"].strip()
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"].strip()
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip().removeprefix("bot")
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
ai_client = genai.Client(api_key=GEMINI_API_KEY)


def get_youtube_clients():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"].strip(),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"].strip(),
        client_secret=os.environ["YT_CLIENT_SECRET"].strip(),
    )
    yt_data = build("youtube", "v3", credentials=creds)
    yt_analytics = build("youtubeAnalytics", "v2", credentials=creds)
    return yt_data, yt_analytics


def send_telegram(message: str, reply_markup: dict = None, parse_mode: str = "Markdown"):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup

    resp = requests.post(url, json=payload)
    if not resp.ok:
        print(f"Telegram API Warning {resp.status_code}: {resp.text}")
        # Fallback to plain text if Markdown formatting fails on special characters
        if parse_mode:
            payload.pop("parse_mode", None)
            resp = requests.post(url, json=payload)
    resp.raise_for_status()
    return resp.json().get("result", {})


def process_telegram_approvals(yt_data):
    """Checks 1-tap inline button presses in Telegram and posts approved replies live to YouTube."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        resp = requests.get(url, params={"timeout": 2}).json()
    except Exception as e:
        print(f"    Telegram update poll skipped: {e}")
        return 0

    if not resp.get("ok") or not resp.get("result"):
        return 0

    posted_count = 0
    max_update_id = 0

    for update in resp["result"]:
        max_update_id = max(max_update_id, update["update_id"])
        cb = update.get("callback_query")
        if not cb:
            continue

        data = cb.get("data", "")
        cb_id = cb["id"]
        msg_id = cb.get("message", {}).get("message_id")

        if data.startswith("post_"):
            comment_id = data.replace("post_", "", 1)
            row = supabase.table("comment_queue").select("*").eq("comment_id", comment_id).execute().data
            if row and row[0]["status"] == "pending_approval":
                reply_text = row[0]["ai_draft_reply"]
                try:
                    yt_data.comments().insert(
                        part="snippet",
                        body={"snippet": {"parentId": comment_id, "textOriginal": reply_text}}
                    ).execute()
                    supabase.table("comment_queue").update({"status": "posted"}).eq("comment_id", comment_id).execute()
                    posted_count += 1
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                        json={"callback_query_id": cb_id, "text": "✅ Reply posted to YouTube!"}
                    )
                    if msg_id:
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageReplyMarkup",
                            json={"chat_id": TELEGRAM_CHAT_ID, "message_id": msg_id, "reply_markup": {"inline_keyboard": []}}
                        )
                except Exception as e:
                    print(f"    Failed posting reply for {comment_id}: {e}")

        elif data.startswith("skip_"):
            comment_id = data.replace("skip_", "", 1)
            supabase.table("comment_queue").update({"status": "ignored"}).eq("comment_id", comment_id).execute()
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                json={"callback_query_id": cb_id, "text": "❌ Skipped"}
            )
            if msg_id:
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageReplyMarkup",
                    json={"chat_id": TELEGRAM_CHAT_ID, "message_id": msg_id, "reply_markup": {"inline_keyboard": []}}
                )

    # Acknowledge processed Telegram updates so they don't repeat
    if max_update_id > 0:
        requests.get(url, params={"offset": max_update_id + 1})

    return posted_count


def clean_metadata_text(text: str) -> str:
    """Fixes common hashtag typos and missing spaces before hashtags."""
    cleaned = text.replace("#healingjourny", "#healingjourney")
    cleaned = re.sub(r"([a-zA-Z0-9])#", r"\1 #", cleaned)
    return cleaned


def auto_hygiene_and_playlist(yt_data, video_obj):
    """Fixes hashtag typos on new uploads and routes them to matching thematic playlists."""
    vid_id = video_obj["id"]
    snip = video_obj["snippet"]
    orig_title = snip["title"]
    orig_desc = snip.get("description", "")
    category_id = snip.get("categoryId", "22")

    new_title = clean_metadata_text(orig_title)
    new_desc = clean_metadata_text(orig_desc)
    actions_taken = []

    if new_title != orig_title or new_desc != orig_desc:
        try:
            yt_data.videos().update(
                part="snippet",
                body={
                    "id": vid_id,
                    "snippet": {
                        "title": new_title,
                        "description": new_desc,
                        "categoryId": category_id,
                        "tags": snip.get("tags", [])
                    }
                }
            ).execute()
            actions_taken.append("Fixed hashtag formatting/typos")
        except Exception as e:
            print(f"    Metadata hygiene skipped for {vid_id}: {e}")

    # Match upload keywords against existing channel playlists
    try:
        pl_res = yt_data.playlists().list(part="snippet", mine=True, maxResults=25).execute()
        title_lower = new_title.lower()
        for pl in pl_res.get("items", []):
            pl_title = pl["snippet"]["title"].lower()
            keywords = [w for w in re.findall(r"[a-z]{4,}", pl_title)]
            if any(kw in title_lower for kw in keywords):
                yt_data.playlistItems().insert(
                    part="snippet",
                    body={
                        "snippet": {
                            "playlistId": pl["id"],
                            "resourceId": {"kind": "youtube#video", "videoId": vid_id}
                        }
                    }
                ).execute()
                actions_taken.append(f"Added to playlist: {pl['snippet']['title']}")
                break
    except Exception as e:
        print(f"    Playlist routing skipped for {vid_id}: {e}")

    return actions_taken


def get_all_upload_ids(yt_data, uploads_id):
    video_ids = []
    next_page_token = None
    while True:
        pl_res = yt_data.playlistItems().list(
            part="contentDetails",
            playlistId=uploads_id,
            maxResults=50,
            pageToken=next_page_token
        ).execute()
        for item in pl_res.get("items", []):
            video_ids.append(item["contentDetails"]["videoId"])
        next_page_token = pl_res.get("nextPageToken")
        if not next_page_token:
            break
    return video_ids


def generate_content_diagnostic(today: str, ranked_videos: list) -> str:
    """Analyzes proven winners vs underperformers to guide original script, hook, audio & subtitle creation."""
    proven = [v for v in ranked_videos if v["views"] >= 500]
    if len(proven) < 5:
        proven = ranked_videos

    winners = sorted(proven, key=lambda x: x["resonance"], reverse=True)[:12]
    losers = sorted(proven, key=lambda x: x["apv"])[:5]

    win_lines = "\n".join(
        [f"- \"{w['title']}\" | Views: {w['views']} | APV: {w['apv']}% | Shares: {w['shares']} | Subs: +{w['subs']}" for w in winners]
    )
    lose_lines = "\n".join(
        [f"- \"{l['title']}\" | Views: {l['views']} | APV: {l['apv']}% | Shares: {l['shares']}" for l in losers]
    )

    prompt = f"""You are the Head Content Strategist for '@bblissthetics' (minimalist dark aesthetic, deep thoughts, emotional self-worth, 25-34 male core audience).
The creator has NOT posted their original content yet and is building an automated pipeline (Agent 2) that generates original hook scripts, AI voiceover audio, synced subtitles, and a '@bblissthetics' watermark.

Analyze these real channel performance metrics:

PROVEN TOP PERFORMERS:
{win_lines}

LOWEST RETENTION VIDEOS:
{lose_lines}

Provide a sharp, concrete diagnostic with 4 exact sections (no fluff, under 280 words total):
1. WINNING HOOK PATTERNS: What exact opening structures/phrases drive >100% loop retention and shares?
2. SCRIPT & PACING FIXES: Why did the lowest retention videos fail, and how should original scripts be structured (word count, pause rhythm, loop ending)?
3. ORIGINAL CONTENT BLUEPRINT (FOR AGENT 2): Exact hook templates, voiceover tone/pacing, and subtitle styling rules for the upcoming original Shorts.
4. TOP 3 THEMES TO DOUBLE DOWN ON: Specific emotional angles for the 25-34 male audience."""

    try:
        res = ai_client.models.generate_content(model="gemini-3.8-flash", contents=prompt)
        analysis_text = res.text.strip()

        supabase.table("creative_insights").upsert({
            "insight_date": today,
            "top_hooks_analysis": analysis_text,
            "script_improvements": lose_lines,
            "winning_themes": [w["title"] for w in winners[:5]],
            "agent2_directives": analysis_text
        }).execute()

        return analysis_text
    except Exception as e:
        print(f"    Content diagnostic skipped ({e})")
        return ""


def run_ops():
    event_name = os.environ.get("GITHUB_EVENT_NAME", "manual")
    print(f"1/6 Authenticating YouTube APIs (Trigger: {event_name})...")
    yt_data, yt_analytics = get_youtube_clients()

    print("2/6 Processing 1-tap Telegram comment approvals...")
    posted_replies = process_telegram_approvals(yt_data)
    if posted_replies:
        print(f"    Posted {posted_replies} approved replies live to YouTube!")

    print("3/6 Checking @bblissthetics uploads, subscriber count & live changes...")
    ch_res = yt_data.channels().list(part="contentDetails,statistics", mine=True).execute()
    channel_item = ch_res["items"][0]
    subs_count = str(channel_item["statistics"].get("subscriberCount", "N/A"))
    uploads_id = channel_item["contentDetails"]["relatedPlaylists"]["uploads"]

    video_ids = get_all_upload_ids(yt_data, uploads_id)
    if not video_ids:
        print("No uploads found.")
        return

    videos = []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i+50]
        vid_res = yt_data.videos().list(part="snippet,statistics", id=",".join(batch)).execute()
        videos.extend(vid_res.get("items", []))
    vid_obj_map = {v["id"]: v for v in videos}

    # --- LIVE CHANGE DETECTION + AUTO-HYGIENE ---
    state_res = supabase.table("channel_state").select("*").eq("id", 1).execute()
    prev_state = state_res.data[0] if state_res.data else None
    live_alerts = []

    if prev_state:
        prev_subs = str(prev_state.get("last_subs_count", ""))
        prev_ids = set(prev_state.get("last_known_video_ids") or [])
        new_uploads = [vid for vid in video_ids if vid not in prev_ids]

        if prev_subs and subs_count != prev_subs:
            live_alerts.append(f"📈 *Subscriber Milestone Update:* `{prev_subs}` → `{subs_count}`")

        for new_id in new_uploads:
            v_obj = vid_obj_map.get(new_id)
            title = v_obj["snippet"]["title"] if v_obj else new_id
            actions = auto_hygiene_and_playlist(yt_data, v_obj) if v_obj else []
            action_note = f"\n⚙️ Auto-Actions: {', '.join(actions)}" if actions else ""
            live_alerts.append(
                f"🎬 *New Upload Detected:* _{title}_{action_note}\nhttps://youtube.com/shorts/{new_id}"
            )

    supabase.table("channel_state").upsert({
        "id": 1,
        "last_subs_count": subs_count,
        "last_known_video_ids": video_ids,
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
    }).execute()

    if live_alerts:
        alert_msg = "🚨 *Live Channel Update (@bblissthetics)*\n\n" + "\n\n".join(live_alerts)
        send_telegram(alert_msg)
        print("Live update alert sent to Telegram!")

    print("4/6 Querying YouTube Analytics (28d video telemetry + YPP & exact sub velocity)...")
    today_dt = datetime.date.today()
    end_date = today_dt.isoformat()
    start_28d = (today_dt - datetime.timedelta(days=28)).isoformat()
    start_90d = (today_dt - datetime.timedelta(days=90)).isoformat()
    start_365d = (today_dt - datetime.timedelta(days=365)).isoformat()

    ana_res = yt_analytics.reports().query(
        ids="channel==MINE",
        startDate=start_28d,
        endDate=end_date,
        metrics="views,engagedViews,averageViewPercentage,likes,shares,subscribersGained,subscribersLost",
        dimensions="video",
        maxResults=200,
        sort="-views"
    ).execute()

    ana_map = {
        r[0]: {
            "views": int(r[1]),
            "engaged_views": int(r[2]),
            "avg_view_percentage": round(float(r[3]), 2),
            "likes": int(r[4]),
            "shares": int(r[5]),
            "subs_gained": int(r[6]),
            "subs_lost": int(r[7])
        }
        for r in ana_res.get("rows", [])
    }

    # Exact 28d Net Subscriber Velocity & YPP Progress
    net_28d_subs = sum((m["subs_gained"] - m["subs_lost"]) for m in ana_map.values())

    ypp_90d_res = yt_analytics.reports().query(
        ids="channel==MINE", startDate=start_90d, endDate=end_date, metrics="engagedViews"
    ).execute()
    shorts_90d_engaged = int(ypp_90d_res["rows"][0][0]) if ypp_90d_res.get("rows") else 0

    ypp_365d_res = yt_analytics.reports().query(
        ids="channel==MINE", startDate=start_365d, endDate=end_date, metrics="estimatedMinutesWatched"
    ).execute()
    watch_hours_365d = round((float(ypp_365d_res["rows"][0][0]) / 60.0), 1) if ypp_365d_res.get("rows") else 0.0

    print("5/6 Writing metadata & telemetry snapshots to Supabase...")
    today = end_date
    best_proven_video = ("None", 0.0, 0)
    total_28d_views = 0
    ranked_videos = []

    for v in videos:
        vid_id = v["id"]
        snip = v["snippet"]
        stats = v.get("statistics", {})
        ana = ana_map.get(vid_id, {})

        supabase.table("videos").upsert({
            "video_id": vid_id,
            "title": snip["title"],
            "description": snip.get("description", ""),
            "tags": snip.get("tags", []),
            "published_at": snip["publishedAt"]
        }).execute()

        apv = ana.get("avg_view_percentage", 0.0)
        shares = ana.get("shares", 0)
        views = ana.get("views", int(stats.get("viewCount", 0)))
        subs_gained = ana.get("subs_gained", 0)
        total_28d_views += ana.get("views", 0)
        resonance = round(apv + (shares / max(views, 1)) * 1000, 2)

        ranked_videos.append({
            "title": snip["title"][:45],
            "views": views,
            "apv": apv,
            "shares": shares,
            "subs": subs_gained,
            "resonance": resonance
        })

        # Filter out <500-view statistical noise when picking top retention winner
        if views >= 500 and apv > best_proven_video[1]:
            best_proven_video = (snip["title"][:35], apv, views)

        supabase.table("video_telemetry").upsert({
            "video_id": vid_id,
            "snapshot_date": today,
            "views": views,
            "engaged_views": ana.get("engaged_views", 0),
            "avg_view_percentage": apv,
            "likes": ana.get("likes", int(stats.get("likeCount", 0))),
            "comments_count": int(stats.get("commentCount", 0)),
            "shares": shares,
            "subs_gained": subs_gained,
            "resonance_score": resonance
        }, on_conflict="video_id,snapshot_date").execute()

    print("6/6 Triaging unanswered comments (max 3/run) with 1-tap Telegram approval...")
    ai_calls = 0
    quota_exhausted = False
    new_drafts_count = 0

    for vid_id in video_ids[:5]:
        if ai_calls >= 3 or quota_exhausted:
            break
        try:
            c_res = yt_data.commentThreads().list(part="snippet", videoId=vid_id, maxResults=10, order="time").execute()
        except Exception:
            continue

        for item in c_res.get("items", []):
            if ai_calls >= 3 or quota_exhausted:
                break
            if item["snippet"]["totalReplyCount"] > 0:
                continue
            top = item["snippet"]["topLevelComment"]
            c_id = top["id"]
            if supabase.table("comment_queue").select("comment_id").eq("comment_id", c_id).execute().data:
                continue

            text = top["snippet"]["textDisplay"]
            prompt = (
                f"Channel '@bblissthetics' (minimalist dark aesthetic, deep thoughts, 25-34 male core audience). "
                f"Comment: '{text}'. Classify strictly as: positive, deep_resonance, neutral, or spam. "
                f"Write a warm, grounded, lowercase reply (max 12 words, 1 subtle emoji max, or NONE if spam).\n"
                f"Output format:\nSENTIMENT: <label>\nREPLY: <text>"
            )
            try:
                res_text = ai_client.models.generate_content(model="gemini-3.8-flash", contents=prompt).text
                ai_calls += 1
            except Exception as e:
                print(f"    Skipping AI comment triage ({e})")
                quota_exhausted = True
                break

            lines = res_text.strip().splitlines()
            sentiment = lines[0].replace("SENTIMENT:", "").strip().lower()
            reply = lines[1].replace("REPLY:", "").strip() if len(lines) > 1 else "NONE"
            if sentiment not in ["positive", "deep_resonance", "neutral", "spam"]:
                sentiment = "neutral"

            msg_id = None
            if sentiment != "spam" and reply != "NONE":
                new_drafts_count += 1
                card = (
                    f"💬 *New Comment on @bblissthetics*\n"
                    f"👤 _{top['snippet']['authorDisplayName']}_: \"{text}\"\n\n"
                    f"✍️ *AI Draft:* `{reply}`"
                )
                buttons = {
                    "inline_keyboard": [[
                        {"text": "✅ Post Reply", "callback_data": f"post_{c_id}"},
                        {"text": "❌ Skip", "callback_data": f"skip_{c_id}"}
                    ]]
                }
                sent_msg = send_telegram(card, reply_markup=buttons)
                msg_id = sent_msg.get("message_id")

            supabase.table("comment_queue").insert({
                "comment_id": c_id,
                "video_id": vid_id,
                "author_name": top["snippet"]["authorDisplayName"],
                "comment_text": text,
                "sentiment": sentiment,
                "ai_draft_reply": reply,
                "status": "ignored" if sentiment == "spam" else "pending_approval",
                "telegram_message_id": msg_id,
                "published_at": top["snippet"]["publishedAt"]
            }).execute()

    # Send full Telemetry + Deep Hook/Script Analysis on manual runs OR at 07:30 IST / 19:30 IST
    current_hour_utc = datetime.datetime.now(datetime.timezone.utc).hour
    is_digest_hour = current_hour_utc in (2, 14)

    if event_name == "workflow_dispatch" or is_digest_hour:
        ypp_shorts_pct = round((shorts_90d_engaged / 3_000_000) * 100, 2)
        ypp_hours_pct = round((watch_hours_365d / 3_000) * 100, 2)

        digest = (
            f"📡 *Agent 1 v2 — Ops & Telemetry ({today})*\n\n"
            f"• *Subscribers (API):* {subs_count} (`{net_28d_subs:+d}` exact net in 28d)\n"
            f"• *Tracked Uploads:* {len(videos)} (28d Views: {total_28d_views:,})\n"
            f"• *Proven Top Retention (500+ views):* _{best_proven_video[0]}_ ({best_proven_video[1]}% across {best_proven_video[2]:,} views)\n"
            f"• *YPP 90d Shorts Views:* {shorts_90d_engaged:,} / 3,000,000 ({ypp_shorts_pct}%)\n"
            f"• *YPP 365d Watch Hours:* {watch_hours_365d:,} / 3,000 ({ypp_hours_pct}%)\n"
            f"• *Comments:* {new_drafts_count} new drafts sent | {posted_replies} approved replies posted"
        )
        send_telegram(digest)

        # Generate & send Deep Content, Script & Hook Diagnostic for Agent 2 & Agent 3
        diagnostic = generate_content_diagnostic(today, ranked_videos)
        if diagnostic:
            diag_msg = f"🧠 *Agent 1 Deep Hook & Script Analysis (Saved for Agent 2)*\n\n{diagnostic}"
            send_telegram(diag_msg, parse_mode=None)

        print("Full v2 digest + deep content analysis sent to Telegram!")
    else:
        print("Background check complete.")


if __name__ == "__main__":
    run_ops()
