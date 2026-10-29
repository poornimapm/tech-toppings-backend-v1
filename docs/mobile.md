# Phones: the Play Store (TWA) and iPhone (PWA)

Tech-Toppings is an installable web app (Phase 11, ADR-0018), so it is on phones already. Two
store paths are below, with what each needs and costs (Phase 13, ADR-0020). Neither is done:
both are the owner's decisions, and the Play Store one is the only step in this project that
isn't free.

## Today: installed from the browser (free)

- **Android (Chrome)**:
  - The browser menu → *Install app*, or the app's own Install button and palette command.
  - Notifications work once turned on under *Notifications*.
- **iPhone and iPad (iOS/iPadOS 16.4 or later)**:
  - Safari → *Share* → *Add to Home Screen*. iOS shows no install prompt, so the app's Install
    button doesn't appear there.
  - Notifications work only in the Home Screen app, once turned on inside it.
  - A Safari tab's storage may be cleared after a week unused; the Home Screen app keeps its
    own. Only drafts and offline items live on the device, and they're sent once online.

Already in place for both (Phase 11):
- HTTPS and a service worker that opens the app offline;
- the manifest: name, `id`, start URL, standalone display, 192 and 512 icons, a maskable 512;
- an Apple touch icon and theme colour;
- safe-area padding for the bottom bar;
- Web Push.

## Android: the Play Store, as a Trusted Web Activity

A Trusted Web Activity (TWA) is a thin Android app that opens the live site full screen in
Chrome. There is no second code base: every web deploy updates the app.

1. **A Google Play developer account.**
   - A one-time registration fee (US$25 when written) and identity verification.
   - New personal accounts must run a closed test (at the time of writing, 12 testers for 14
     days) before they can publish. Check the current rules in Play Console.
2. **The app package**, made with Google's Bubblewrap CLI from the live manifest:

   ```powershell
   npx --yes @bubblewrap/cli init --manifest=https://<project>.pages.dev/manifest.webmanifest
   npx --yes @bubblewrap/cli build
   ```

   - It offers to download the JDK and Android SDK it needs.
   - It asks for a package name: the site's address reversed, e.g.
     `dev.pages.<project_name>.twa`. Each part must start with a letter and use only letters,
     digits and `_`, so a `-` in the project's name becomes `_`. The name is permanent.
   - **Signing**: new Play apps must use Play App Signing. Google holds the key that signs what
     people install; the key Bubblewrap makes (`android.keystore`) is your *upload* key. Keep
     it and its passwords safe, outside the repository (a lost upload key can be reset from
     Play Console, but that takes a few days).
3. **Digital Asset Links**, so Chrome trusts the app to show the site without its address bar.
   - The site serves `/.well-known/assetlinks.json` naming the package and the SHA-256
     fingerprints of both keys (Play Console → *Test and release* → *App integrity*):
     - the app signing key's, for the app people install from Play;
     - the upload key's, for a build you install yourself to test.
   - Put it in the frontend at `public/.well-known/assetlinks.json`. Pages serves it as it is,
     and the service worker never stores dot folders.

   ```json
   [
     {
       "relation": ["delegate_permission/common.handle_all_urls"],
       "target": {
         "namespace": "android_app",
         "package_name": "<your package name>",
         "sha256_cert_fingerprints": ["<app signing key's>", "<upload key's>"]
       }
     }
   ]
   ```

   - Check it with
     `https://digitalassetlinks.googleapis.com/v1/statements:list?source.web.site=https://<project>.pages.dev&relation=delegate_permission/common.handle_all_urls`.
4. **The store listing**:
   - name and descriptions (English, Tamil, Hindi), and screenshots;
   - the privacy policy's address (`privacy-policy-template.md`, filled in and published);
   - the Data Safety form (the template's table answers it);
   - the content rating and target audience.
5. **Review**: Play reviews each release. Web changes need no new release; a new icon, name or
   package setting does.
6. **Once a year**: Play requires apps to target a recent Android version, and stops offering
   updates of one that falls behind. Update the wrapper and release it again:

   ```powershell
   npx --yes @bubblewrap/cli update
   npx --yes @bubblewrap/cli build
   ```

   Then upload the new bundle as a release. Play Console warns before the deadline.

A custom domain isn't needed: a TWA works with `*.pages.dev`. The site and the package are
paired for good, though: moving to another domain later means a new asset links file and an
app update.

## iPhone: stay a Home Screen web app

- **The App Store** is not recommended now:
  - it needs the Apple Developer Program (US$99 a year), a Mac with Xcode, and App Review;
  - App Review usually rejects an app that only wraps a website (guideline 4.2, minimum
    functionality).
- **The Home Screen web app is the path.** Tell people to add it from Safari (*Share* → *Add to
  Home Screen*) and to turn notifications on inside it. The push card already says the Home
  Screen is needed on iPhone (Phase 11).

## What to watch

- The fees, the closed-test rule and Apple's guideline change: check them before starting.
- Store listings need texts in every language the app ships (en, ta, hi), like the app itself.
