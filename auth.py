import json
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

flow = InstalledAppFlow.from_client_secrets_file(
    "client_secret.json",
    SCOPES,
)

credentials = flow.run_local_server(
    port=0,
    access_type="offline",
    prompt="consent",
)

if not credentials.refresh_token:
    raise RuntimeError(
        "Google did not provide a refresh token."
    )

Path("token.json").write_text(
    credentials.to_json(),
    encoding="utf-8",
)

print("Authorization successful!")
print("Copy token.json into a GitHub Actions secret.")
