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

## স্ট্যাটাস: FA-B3 সম্পূর্ণ (গ্রুপ B সম্পূর্ণ)

**গ্রুপ B (orchestration wrapper, F3 সহ, দুটো পাথ) সম্পূর্ণ ও fully tested,
standalone (app.py এখনো অপরিবর্তিত)।** `chunk-FA-B3-done` ট্যাগ বসানো
হয়েছে।

এই চাংকে যা হলো (শুধু robustness + টেস্ট, নতুন ফিচার না):
- `run_auto_tts_chain()` / `run_user_upload_chain()` — দুটোতেই ভেতরের
  প্রতিটা ধাপ থেকে আসা exception (FileNotFoundError, ValueError,
  RuntimeError, `auto_cut.DraftValidationError`) caller পর্যন্ত propagate
  হয়; কোনো silent swallow / bare `except: pass` নেই; মাঝ-চেইনে কোনো ধাপ
  ব্যর্থ হলে পরের ধাপ চালানো হয় না।
- `pipeline/full_auto_chain.py`-এ কোনো TODO/placeholder নেই।
- `test_full_auto_chain.py` এখন সম্পূর্ণ failure-case টেস্ট স্যুট (৬টা):
  happy path (দুটো), TTS total fail, draft validation fail, final render
  fail, D3 align fail — প্রতিটাতে exception propagate + `final_video.mp4`
  তৈরি না হওয়া যাচাই করা হয়। পুরো স্যুট এখন **২৭৯টা টেস্ট OK**
  (২৭৫ + ৪)।

## পরের কাজ

**গ্রুপ C (auto_tts পাথ HTTP-ওয়্যারিং, FA-C1 থেকে)।** বিস্তারিত
`docs/FA_CHUNK_BATCH.md`-এর `--- CHUNK FA-C1 ---` সেকশনে। সবচেয়ে ঝুঁকিপূর্ণ
চাংক — existing background-thread wiring বদলাচ্ছে:
- `_run_upload_pipeline(job_id)`-এর একদম শেষে (upload_pipeline done লেখার
  ঠিক পরে) `voice_source == "auto_tts"` হলে একই থ্রেডে (নতুন থ্রেড নয়)
  `full_auto_chain.run_auto_tts_chain()` চালিয়ে `auto_full_render` stage-এ
  running/done/error লিখতে হবে; `user_upload` পাথ স্পর্শ করা যাবে না।
- টেস্ট: HTTP TestClient দিয়ে POST /upload (auto_tts) → শুধু status পোল →
  `outputs/<job_id>/final_video.mp4` তৈরি; user_upload কেসে
  `upload_pipeline`/`done`-এই থামা যাচাই।

পুরো data-flow/চলার নিয়ম: `docs/FINAL_SUMMARY.md`। চ্যাঞ্জলগ:
`docs/CHANGELOG.md`।
