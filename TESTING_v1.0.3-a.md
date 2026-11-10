# v1.0.3-a test installer / 検証用

Base: main commit 3b33f0dad814008d65f294f9f12579827a0a360e.
This is a manual test build, not a stable release or automatic update.

## Included changes / 変更内容
- TOP stop: normal recording finalization first; explicit second click cancels remaining work. Rapid double clicks are guarded for 0.6 seconds.
- Default new audio source: microphone + PC playback. Previously saved choices are retained.
- Japanese/English display language: first installer screen and application settings; OS language initializes new installs, unsupported languages use English, unavailable locale uses Japanese. Speech-recognition language is separate. Some advanced messages are still Japanese.
- Check Windows global shortcut availability before accepting and saving a shortcut.
- More tolerant automatic-update timeouts, download progress and cancellation.

## Test safely / 利用上の注意
Prefer a separate test installation folder and a separate output folder. Before upgrading an existing installation, export settings and back up recordings/transcripts.
This test executable is not Authenticode code-signed. Do not disable endpoint security; if organizational policy blocks execution, use an approved environment or ask its administrator.
A Defender scan and installer smoke test are required before this executable is uploaded. These checks do not guarantee compatibility with every PC, microphone or security product.
No stable release, existing tag or public release asset is replaced by this workflow.
