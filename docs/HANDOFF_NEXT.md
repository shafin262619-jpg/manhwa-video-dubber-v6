# HANDOFF NEXT

## প্রজেক্ট: manhwa-video-dubber-v6 (Full-Auto Pipeline)

এই রিপো `manhwa-video-dubber`-এর version-bump কপি (পুরনো রিপো:
https://github.com/shafin262619-jpg/manhwa-video-dubber — অক্ষত রাখা
হয়েছে)। পুরো commit history + পুরনো ট্যাগ
(`manhwa-video-dubber-v6-robustness-final` ইত্যাদি) এখানে প্রিজার্ভ
করা আছে।

বর্তমানে কাজ চলছে **Full-Auto Pipeline** প্ল্যানে — ধারাবাহিক চাংক
FA-A1 → FA-B1 → FA-B2 → FA-B3 → FA-C1 → FA-C2 → FA-D1 → FA-D2 →
FA-E1 → FA-E2 → FA-F1 → FA-F2। প্রতিটা চাংকের বিস্তারিত প্রম্পট:
`docs/FA_CHUNK_BATCH.md`। প্রতিটা চাংক শেষে `chunk-FA-<id>-done` ট্যাগ
বসানো হবে।

## স্ট্যাটাস: গ্রুপ D সম্পূর্ণ — PRD-এর মূল requirement এখন পুরোপুরি বাস্তবায়িত

**গ্রুপ D সম্পূর্ণ — auto_tts পাথে zero-click, user_upload পাথে ঠিক একটাই
থামা (অডিও আপলোড); অডিও দেওয়ার পর ইউজার আর কোনো ক্লিক ছাড়াই ফাইনাল
ভিডিও পান।** `chunk-FA-D2-done` ট্যাগ বসানো হয়েছে।

এই চাংকে যা হলো:
- `upload_voiceover()` — অডিও সেভের পর পুরনো "Voiceover saved — Align
  subtitles" পেজের বদলে `user_audio_pipeline`/`running` লিখে একটা daemon
  thread শুরু করে (`_start_stage(job_id, "user_audio_pipeline",
  _run_user_audio_pipeline)`) যা `run_user_upload_chain(job_id)` (D3→D4→E1→E2→F3)
  চালিয়ে `done` (result সহ) বা `error` (friendly detail) লেখে; তারপর
  existing `_polling_page(..., "user_audio_pipeline")` রিটার্ন করে।
- `upload_status_page()` — user_upload পাথে `user_audio_pipeline`/`done` হলে
  `_render_chain_final_result()` (FA-C2-এর auto_full_render-এর মতোই একই
  adapter) দিয়ে ফাইনাল ভিডিও + ডাউনলোড লিংক; `running` হলে পোলিং পেজ।
- Hard constraints: `/voiceover/{job_id}/align_uploaded` রুট ডিলিট করা হয়নি
  (existing `test_align_page_*` টেস্টে verify হয়); থ্রেড কখনো uncaught
  exception-এ মরে না (try/except + `except Exception`, daemon-thread
  convention); `UnsupportedAudioError` ভ্যালিডেশন অপরিবর্তিত।
- টেস্ট: +১টা HTTP end-to-end — POST /upload (user_upload) → B1/B2/C1 পোল →
  POST /voiceover/{id}/upload (fake wav) → `user_audio_pipeline` done পোল →
  `GET /download/{job_id}` কন্টেন্ট ফেরায় (শুধু ওই endpoint গুলোই)। পুরনো
  `test_voiceover_upload.py` upload-page টেস্ট এখন auto-continue পোলিং পেজ
  যাচাই করে। পুরো স্যুট এখন **২৮৪টা টেস্ট OK** (২৮৩ + ১ নতুন, ১ আপডেট)।

## পরের কাজ

**গ্রুপ E (ব্যাকওয়ার্ড-কম্প্যাট অডিট + নতুন E2E রিগ্রেশন টেস্ট)।**
বিস্তারিত `docs/FA_CHUNK_BATCH.md`-এর `--- CHUNK FA-E1 ---` সেকশনে:
- FA-E1: backward-compat অডিট — পুরনো ম্যানুয়াল রুটগুলো
  (`/voiceover/{job_id}/choose`, `/voiceover/{job_id}/auto_tts`,
  `/voiceover/{job_id}/upload`, `/voiceover/{job_id}/align_uploaded`,
  `/final/{job_id}`, `/review/{job_id}`) নতুন FA পাইপলাইনের সাথে
  coexist করছে যাচাই; app.py-তে কোনো dead/broken রেফারেন্স নেই।
- FA-E2: permanent E2E regression টেস্ট স্যুট — দুটো পাথের
  upload→final_video সম্পূর্ণ সাইকেল permanent টেস্ট হিসেবে।

পুরো data-flow/চলার নিয়ম: `docs/FINAL_SUMMARY.md`। চ্যাঞ্জলগ:
`docs/CHANGELOG.md`।
