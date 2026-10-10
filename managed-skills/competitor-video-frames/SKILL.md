---
name: competitor-video-frames
description: "Extract, verify with vision, and embed competitor UI frames from YouTube videos. Rejects talking heads, slides, and intro cards using inspect_image before uploading with gh image."
---

> Source of truth: [`managed-skills/competitor-video-frames/SKILL.md`](https://github.com/Wladefant/super-board/blob/main/managed-skills/competitor-video-frames/SKILL.md) in Wladefant/super-board. Edit it there and merge. Then `python scripts/install-managed-skills.py competitor-video-frames` copies it to `~/.veyyon/profiles/default/agent/managed-skills/`. The installer backs up a local edit and then overwrites it.

# Competitor Video Frames

Use this skill when you extract screenshots from YouTube videos for competitor research.
Follow this procedure to prove that images show product user interfaces.

## Defect to avoid

Lanes often extract video frames at blind timestamps.
They post presenter talking heads or title cards instead of product UI.
An example is https://github.com/Wladefant/shipnovo/issues/680 (Sendcloud Pack & Go).
Never post human faces, slides, or intro cards as product evidence.

Second defect (Linnworks, https://github.com/Wladefant/shipnovo/issues/677): the frames were a browser PDF viewer
showing a generated label plus a print-success toast, captioned as the "Open Orders table" and "batch print dialog".
That is `OTHER`, not the app screen. Also check these:
1. A cited video id may be real but on a third-party channel (Brainence, Ecomclips) while the issue says "official".
   Read title and channel with `https://www.youtube.com/oembed?format=json&url=https://www.youtube.com/watch?v=<id>` and label the source.
2. The caption must claim only what the frame shows (no "payment status", "order age" or "sync view" unless visible).
3. A quote attributed to a short demo video with no reviews is "No verified quote".
4. On Windows, `yt-dlp` 720p dash gets HTTP 403. Run `yt-dlp -F <url>` and download the m3u8 video-only 720p format (e.g. `-f 232`).
   Give ffmpeg `stdin=subprocess.DEVNULL` and put `-ss` after `-i`.
5. Attachments of private repos need `Authorization: token <gh auth token>`; drop the header on the redirect to another host.
6. Screen-scan fast: tile one frame per 6 s into 3x3 sheets labelled `t=Ns`, classify the sheet, then re-classify the chosen single frame.

## Procedure

### 1. Prerequisites
1. Check that `yt-dlp` and `ffmpeg` exist on the system.
2. Set timeouts on every subprocess command.
3. On Windows, pass `creationflags=subprocess.CREATE_NO_WINDOW` in Python.
4. Pass `windowsHide: true` in Node.
5. Create a clean temporary directory under `C:/Users/wkiri/.veyyon/tmp/frames-audit/`.

### 2. Video download
1. Search public videos with `yt-dlp`:
   `yt-dlp "ytsearch5:<query>" --flat-playlist --print "%(id)s %(duration)s %(title)s"`
2. Download video at 720p or lower:
   `yt-dlp -f "bv*[height<=720]+ba/b[height<=720]" -o "<temp_dir>/%(id)s.%(ext)s" <url>`

### 3. Extract candidate frames
1. Extract candidate frames around the target timestamp with `ffmpeg`:
   `ffmpeg -ss <timestamp> -i <video_file> -frames:v 1 <temp_dir>/candidate.png`
2. Sample nearby frames every 2 seconds when scanning a section.

### 4. Vision check with inspect_image
1. Call `inspect_image` on each candidate frame before upload.
2. Provide the strict question:
   `Does this frame show the product UI described as '<caption>'? Answer UI / TALKING_HEAD / SLIDE / OTHER, plus one line of what is visible.`
3. Accept only frames that receive a `UI` verdict.
4. Reject any frame classified as `TALKING_HEAD`, `SLIDE`, or `OTHER`.
5. If rejected, sample frames every 2 seconds within +/-60 seconds of the timestamp.
6. Stop when a candidate frame matches the target UI.
7. If no candidate frame shows the UI, state that the video lacks the screen.
8. Omit the image when the video has no matching screen.

### 5. Upload with gh image
1. Upload accepted UI frames with `gh image`:
   `gh image <path_to_frame.png> --repo <owner/repo>`
2. Obtain the URL matching `https://github.com/user-attachments/assets/<uuid>`.
3. Never use third-party hosts or `raw.githubusercontent.com`.

### 6. Embed in Markdown
1. Embed the image with Markdown image syntax:
   `![<caption or alt text>](https://github.com/user-attachments/assets/<uuid>)`
2. Never use raw HTML `<img>` tags in issue bodies or comments.

### 7. Verify image load
1. Fetch the rendered issue or comment through GitHub API:
   `gh api repos/<owner>/<repo>/issues/comments/<id> -H "Accept: application/vnd.github.html+json"`
2. Verify that every image URL returns HTTP status 200 with content type `image/*`.

### 8. Record evidence
1. Record the `inspect_image` verdict line for each frame in the evidence text.
2. Include the timestamped source video URL.

### 9. Clean temporary files
1. Delete all downloaded videos and temporary frame files before completing the task.
