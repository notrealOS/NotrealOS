# Connect the gallery to Google Photos (one time, ~2 min)

Use the same Google Cloud project as the Maps API key: https://console.cloud.google.com

1. **APIs & Services → Library**: enable **Photos Library API** and **Google Photos Picker API**.
2. **OAuth consent screen** (Google Auth Platform): User type *External*, app name "notarealGallery",
   your email as support/contact. Under **Audience → Test users** add your own Google account.
   (Leaving the app in *Testing* is fine for personal use. Google may ask you to sign in
   again every 7 days in Testing mode. After sign-in works, **Audience → Publish app** removes
   that; Google shows an "unverified app" warning once, tap Advanced → continue.)
3. **Credentials → Create credentials → OAuth client ID → Application type: Desktop app**
   (NOT "Web application": a Web client rejects the phone's 127.0.0.1 sign-in callback).
   Download the JSON.
4. Get it into the phone's **~/Downloads** (download it on the phone, or scp it from the PC).
   Gallery moves `client_secret_*.json` to `~/.config/gallery/client.json` by itself.
5. In Gallery tap **IMPORT** or the cloud button: Firefox opens Google's sign-in, allow access,
   then the tab says "Signed in".

What the gallery can do with Google (Google's 2025 rules):
- **Backup**: uploads camera photos/videos into the album "notarealPhone" (Wi-Fi only by default).
- **IMPORT**: opens Google's Photos Picker; whatever you pick is downloaded to ~/Pictures/Google.
- It can NOT browse your whole Google library on its own (Google no longer allows that for any app).
