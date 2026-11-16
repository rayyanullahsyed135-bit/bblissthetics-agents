import os
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
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
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

def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    )
    resp.raise_for_status()

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

def run_ops():
    event_name = os.environ.get("GITHUB_EVENT_NAME", "manual")
    print(f"1/5 Authenticating YouTube APIs (Trigger: {event_name})...")
    yt_data, yt_analytics = get_youtube_clients()

    print("2/5 Checking @bblissthetics uploads and subscriber count...")
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
    vid_title_map = {v["id"]: v["snippet"]["title"] for v in videos}

    # --- LIVE CHANGE DETECTION ---
    state_res = supabase.table("channel_state").select("*").eq("id", 1).execute()
    prev_state = state_res.data[0] if state_res.data else None
    live_alerts = []

    if prev_state:
        prev_subs = str(prev_state.get("last_subs_count", ""))
        prev_ids = set(prev_state.get("last_known_video_ids") or [])
        new_uploads = [vid for vid in video_ids if vid not in prev_ids]

        if prev_subs and subs_count != prev_subs:
            live_alerts.append(f"📈 *Subscriber Update:* `{prev_subs}` → `{subs_count}`")

        for new_id in new_uploads:
            title = vid_title_map.get(new_id, new_id)
            live_alerts.append(f"🎬 *New Upload Detected:* _{title}_\nhttps://youtube.com/shorts/{new_id}")

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

    print("3/5 Querying private 28-day YouTube Analytics...")
    end_date = datetime.date.today().isoformat()
    start_date = (datetime.date.today() - datetime.timedelta(days=28)).isoformat()
   ana_res = yt_analytics.reports().query(
        ids="channel==MINE",
        startDate=start_date,
        endDate=end_date,
        metrics="views,engagedViews,averageViewPercentage,likes,shares,subscribersGained",
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
            "subs_gained": int(r[6])
        }
        for r in ana_res.get("rows", [])
    }

    print("4/5 Writing metadata & telemetry snapshots to Supabase...")
    today = datetime.date.today().isoformat()
    best_video = ("None", 0.0)
    total_28d_views = 0

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
        total_28d_views += ana.get("views", 0)
        resonance = round(apv + (shares / max(views, 1)) * 1000, 2)

        if apv > best_video[1]:
            best_video = (snip["title"][:35], apv)

        supabase.table("video_telemetry").upsert({
            "video_id": vid_id,
            "snapshot_date": today,
            "views": views,
            "engaged_views": ana.get("engaged_views", 0),
            "avg_view_percentage": apv,
            "likes": ana.get("likes", int(stats.get("likeCount", 0))),
            "comments_count": int(stats.get("commentCount", 0)),
            "shares": shares,
            "subs_gained": ana.get("subs_gained", 0),
            "resonance_score": resonance
        }, on_conflict="video_id,snapshot_date").execute()

    print("5/5 Triaging unanswered comments with Gemini 3.8 Flash...")
    drafted_samples = []
    for vid_id in video_ids[:15]:
        try:
            c_res = yt_data.commentThreads().list(part="snippet", videoId=vid_id, maxResults=15, order="time").execute()
        except Exception:
            continue

        for item in c_res.get("items", []):
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
            res_text = ai_client.models.generate_content(model="gemini-3.8-flash", contents=prompt).text
            lines = res_text.strip().splitlines()
            sentiment = lines[0].replace("SENTIMENT:", "").strip().lower()
            reply = lines[1].replace("REPLY:", "").strip() if len(lines) > 1 else "NONE"
            if sentiment not in ["positive", "deep_resonance", "neutral", "spam"]:
                sentiment = "neutral"

            supabase.table("comment_queue").insert({
                "comment_id": c_id,
                "video_id": vid_id,
                "author_name": top["snippet"]["authorDisplayName"],
                "comment_text": text,
                "sentiment": sentiment,
                "ai_draft_reply": reply,
                "status": "ignored" if sentiment == "spam" else "pending_approval",
                "published_at": top["snippet"]["publishedAt"]
            }).execute()

            if sentiment != "spam":
                drafted_samples.append(f"• _{text[:40]}_ → `{reply}`")

    # Send full summary digest on manual runs OR at 07:30 IST / 19:30 IST (02:00 / 14:00 UTC)
    current_hour_utc = datetime.datetime.now(datetime.timezone.utc).hour
    is_digest_hour = current_hour_utc in (2, 14)

    if event_name == "workflow_dispatch" or is_digest_hour:
        sample_block = "\n".join(drafted_samples[:3]) if drafted_samples else "• No new unanswered comments."
        digest = (
            f"📡 *Agent 1 Report ({today})*\n\n"
            f"• *Subscribers (API):* {subs_count}\n"
            f"• *Tracked Uploads (Shorts + Videos):* {len(videos)} (28d Views: {total_28d_views:,})\n"
            f"• *Top Loop Retention:* _{best_video[0]}_ ({best_video[1]}%)\n"
            f"• *New Comment Replies Drafted:* {len(drafted_samples)}\n\n"
            f"*Sample AI Replies (Saved in Supabase):*\n{sample_block}"
        )
        send_telegram(digest)
        print("Full summary digest sent.")
    else:
        print("Background check complete (no digest scheduled this run).")

if __name__ == "__main__":
    run_ops()
