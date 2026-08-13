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

## স্ট্যাটাস: FA-C1 সম্পূর্ণ (auto_tts পাথ এখন end-to-end wired)

**গ্রুপ C-এর সবচেয়ে ঝুঁকিপূর্ণ চাংক (existing background-thread wiring
বদলানো) সম্পূর্ণ ও fully tested।** `chunk-FA-C1-done` ট্যাগ বসানো হয়েছে।

এই চাংকে যা হলো:
- `app.py`-এর `_run_upload_pipeline(job_id)` — পুরনো B1→B2→C1 চেইন শেষে
  `upload_pipeline`/`done` লেখার পরে, `voice_source == "auto_tts"` হলে **একই
  থ্রেডেই** (নতুন থ্রেড নয়) `auto_full_render`/`running` →
  `full_auto_chain.run_auto_tts_chain(job_id, call_budget)` →
  `auto_full_render`/`done` (result attached) চলে; ব্যর্থ হলে
  FileNotFoundError / ValueError / RuntimeError / `auto_cut.DraftValidationError`
  → `auto_full_render`/`error` (friendly message), এবং শেষের `except Exception`
  দিয়ে নিশ্চিত করা হয়েছে daemon thread কখনো uncaught exception-এ মরে না যায়।
- `user_upload` পাথ (ও কোনো choice ছাড়া পুরনো job) স্পর্শ করা হয়নি —
  এখনো `upload_pipeline`/`done`-এই থামে; গ্রুপ D সেটা wire করবে।
- টেস্ট: `AutoFullRenderWireTest` (২টা HTTP TestClient টেস্ট) — auto_tts →
  শুধু `/api/jobs/{id}/status` পোল করে `auto_full_render` done +
  `outputs/<job_id>/final_video.mp4` তৈরি যাচাই; user_upload →
  `upload_pipeline`/`done`-এই থামা + কোনো `auto_full_render` stage নেই +
  final_video নেই। পুরো স্যুট এখন **২৮১টা টেস্ট OK** (২৭৯ + ২)।

## পরের কাজ

**FA-C2 (auto_tts-এর upload status/polling পেজ পোলিশ)।** বিস্তারিত
`docs/FA_CHUNK_BATCH.md`-এর `--- CHUNK FA-C2 ---` সেকশনে। FA-C1-এ
`auto_full_render` stage এর data সঠিকভাবে লেখা হচ্ছে; FA-C2-এ:
- `/upload/{job_id}/status` পেজ (polling page) চালু অবস্থায়
  `auto_full_render` stage দেখাতে হবে; `done` হলে
  `/job/{job_id}/result`-এ যাওয়ার নির্দেশ; `error` হলে friendly message।
- টেস্ট: user_upload-এ কোনো `auto_full_render` UI না দেখানো,
  auto_tts-এ `done`/`error` UI সঠিকভাবে দেখানো যাচাই।

পুরো data-flow/চলার নিয়ম: `docs/FINAL_SUMMARY.md`। চ্যাঞ্জলগ:
`docs/CHANGELOG.md`।
