import json
import mimetypes
import os
import subprocess
import tempfile
import time
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

STATE_PATH = Path("state.json")
SOURCE = os.environ["SOURCE_CHANNEL_ID"].strip()
PRIVACY = os.getenv("UPLOAD_PRIVACY", "private")
MAX_PER_RUN = 2

if PRIVACY not in {"private", "unlisted", "public"}:
    raise ValueError("Invalid UPLOAD_PRIVACY")

credentials = Credentials.from_authorized_user_info(
    json.loads(os.environ["YT_TOKEN_JSON"]),
    SCOPES,
)

youtube = build(
    "youtube",
    "v3",
    credentials=credentials,
    cache_discovery=False,
)


def save_state(state):
    STATE_PATH.write_text(
        json.dumps(state, indent=2),
        encoding="utf-8",
    )


def uploads_playlist(channel_id=None, mine=False):
    args = {
        "part": "contentDetails",
    }
    if mine:
        args["mine"] = True
    else:
        args["id"] = channel_id

    result = youtube.channels().list(**args).execute()
    items = result.get("items", [])

    if not items:
        raise RuntimeError("YouTube channel not found")

    return items[0]["contentDetails"][
        "relatedPlaylists"
    ]["uploads"]


def playlist_page(playlist_id, page_token=None):
    return youtube.playlistItems().list(
        part="contentDetails",
        playlistId=playlist_id,
        maxResults=50,
        pageToken=page_token,
    ).execute()


def get_new_ids(playlist_id, known):
    found = []
    page_token = None

    while True:
        page = playlist_page(playlist_id, page_token)

        for item in page.get("items", []):
            vid = item["contentDetails"].get("videoId")

            if not vid:
                continue

            if vid in known:
                return found

            found.append(vid)

        page_token = page.get("nextPageToken")
        if not page_token:
            break

    return found


def get_metadata(video_id):
    result = youtube.videos().list(
        part="snippet,liveStreamingDetails",
        id=video_id,
    ).execute()

    items = result.get("items", [])
    return items[0] if items else None


def already_uploaded(video_id):
    """Check the most recent 150 destination uploads."""
    playlist_id = uploads_playlist(mine=True)
    token = None
    marker = f"Source video ID: {video_id}"

    for _ in range(3):
        page = playlist_page(playlist_id, token)
        ids = [
            item["contentDetails"]["videoId"]
            for item in page.get("items", [])
            if item.get("contentDetails", {}).get("videoId")
        ]

        if ids:
            result = youtube.videos().list(
                part="snippet",
                id=",".join(ids),
            ).execute()

            for item in result.get("items", []):
                description = item.get(
                    "snippet", {}
                ).get("description", "")

                if marker in description:
                    return True

        token = page.get("nextPageToken")
        if not token:
            break

    return False


def download_and_upload(video_id, metadata):
    url = f"https://www.youtube.com/watch?v={video_id}"
    snippet = metadata["snippet"]

    suffix = (
        f"\n\nSource: {url}"
        f"\nSource video ID: {video_id}"
    )

    description = (
        snippet.get("description", "")[
            :5000 - len(suffix)
        ] + suffix
    )

    body = {
        "snippet": {
            "title": snippet["title"][:100],
            "description": description,
            "categoryId": snippet.get(
                "categoryId", "22"
            ),
            "tags": snippet.get("tags", []),
        },
        "status": {
            "privacyStatus": PRIVACY,
        },
    }

    with tempfile.TemporaryDirectory() as folder:
        pattern = str(
            Path(folder) / "source.%(ext)s"
        )

        print("Downloading:", url, flush=True)

        subprocess.run(
            [
                "yt-dlp",
                "--no-playlist",
                "--no-progress",
                "-f",
                "bv*[ext=mp4]+ba[ext=m4a]"
                "/b[ext=mp4]/bv*+ba/b",
                "--merge-output-format",
                "mp4",
                "-o",
                pattern,
                url,
            ],
            check=True,
        )

        valid_extensions = {
            ".mp4", ".mkv", ".webm", ".mov"
        }

        files = [
            path
            for path in Path(folder).iterdir()
            if path.is_file()
            and path.suffix.lower() in valid_extensions
        ]

        if len(files) != 1:
            raise RuntimeError(
                "Exactly one video file was expected"
            )

        path = files[0]
        mime = mimetypes.guess_type(
            str(path)
        )[0] or "application/octet-stream"

        media = MediaFileUpload(
            str(path),
            mimetype=mime,
            chunksize=8 * 1024 * 1024,
            resumable=True,
        )

        request = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )

        result = None
        retries = 0

        while result is None:
            try:
                progress, result = request.next_chunk()
                if progress:
                    percent = progress.progress() * 100
                    print(
                        f"Upload: {percent:.1f}%",
                        flush=True,
                    )
            except HttpError as exc:
                status = exc.resp.status
                if (
                    status in {429, 500, 502, 503, 504}
                    and retries < 5
                ):
                    time.sleep(min(60, 2 ** retries))
                    retries += 1
                else:
                    raise

        print(
            "Uploaded:",
            f"https://youtu.be/{result['id']}",
            flush=True,
        )


def main():
    source_playlist = uploads_playlist(SOURCE)

    if not STATE_PATH.exists():
        # First run: skip existing older videos.
        first_page = playlist_page(source_playlist)
        existing = [
            item["contentDetails"]["videoId"]
            for item in first_page.get("items", [])
            if item.get("contentDetails", {}).get("videoId")
        ]

        state = {
            "seen": existing,
            "pending": existing[:1],
            "uploaded": [],
        }

        save_state(state)
        print(
            "Initialized. Only the latest existing "
            "video will be tested."
        )
    else:
        state = json.loads(
            STATE_PATH.read_text(encoding="utf-8")
        )

        known = set(state["seen"])
        new_ids = get_new_ids(
            source_playlist, known
        )

        for video_id in reversed(new_ids):
            state["seen"].append(video_id)
            state["pending"].append(video_id)

        if new_ids:
            print(
                f"Found {len(new_ids)} new videos"
            )
        save_state(state)

    if not state["pending"]:
        print("No pending videos")
        return

    completed = 0
    failures = []

    for video_id in list(state["pending"]):
        if completed >= MAX_PER_RUN:
            break

        try:
            metadata = get_metadata(video_id)

            if not metadata:
                print("Not available yet:", video_id)
                continue

            live_status = metadata["snippet"].get(
                "liveBroadcastContent", "none"
            )

            if live_status in {"live", "upcoming"}:
                print(
                    "Waiting for livestream:",
                    video_id,
                )
                continue

            if already_uploaded(video_id):
                print(
                    "Duplicate detected:",
                    video_id,
                )
            else:
                download_and_upload(
                    video_id, metadata
                )

            state["pending"].remove(video_id)
            state["uploaded"].append(video_id)
            save_state(state)
            completed += 1

        except Exception as exc:
            print(
                f"Failed {video_id}: {exc}",
                flush=True,
            )
            failures.append(video_id)

    if failures:
        raise RuntimeError(
            "Retry next run: " + ", ".join(failures)
        )

    print("Mirror job completed")


if __name__ == "__main__":
    main()
