# HANDOFF NEXT

## প্রজেক্ট: manhwa-video-dubber-v2

এই রিপো `manhwa-video-dubber`-এর version-bump কপি (পুরনো রিপো:
https://github.com/shafin262619-jpg/manhwa-video-dubber — অক্ষত রাখা
হয়েছে, ডিলিট/মডিফাই করা হয়নি)। পুরো commit history + সব পুরনো ট্যাগ
(S1–G2, UI1, UI2, manhwa-video-dubber-final ইত্যাদি) এখানে প্রিজার্ভ
করা আছে।

## স্ট্যাটাস: সম্পূর্ণ

**Robustness-আপডেট প্ল্যান (U0–U5) সম্পূর্ণ।** চাংক-ক্রম U0 → U1a → U1b
→ U1c → U2a → U2b → U3a → U3b → U4 → U5 — সবগুলো DONE, প্রতিটা চাংকে
`chunk-<id>-done` ট্যাগ বসানো হয়েছে এবং শেষে `manhwa-video-dubber-
robustness-final` ট্যাগও যোগ করা হয়েছে।

ফুল টেস্ট স্যুট **২৭০টা টেস্ট পাশ করে** (`python3 -m unittest discover
-s pipeline/tests -v`) — U0-এর পরে ২০৯ ছিল, বাকিগুলো চাংকগুলোতে যোগ হয়েছে
(U1a +১০, U1b ২১৯, U1c +২, U2a +২৩, U2b +৩, U3a +৩, U3b +২, U4 +৫, U5
+১৩)। zero regression।

## চাংক-গ্রুপ সারসংক্ষেপ (কোন গ্রুপ কী যোগ করলো)

- **U0** — `TTS_MODEL = gemini-3.1-flash-tts-preview` কমেন্ট + pin-টেস্ট
  (deprecation replacement)। (নোট: "pro-tier paid-only / flash free tier"
  দাবি এই রিপোতে যাচাই করা যায়নি — ব্যবহারকারীর কাছ থেকে ভবিষ্যতে
  ভেরিফাই করতে হবে।)
- **U1a** — job-status infrastructure (`pipeline/job_status.py`) + read-only
  polling এন্ডপয়েন্ট `GET /api/jobs/{job_id}/status`।
- **U1b** — `/upload` background + idempotent; `_run_upload_pipeline`
  (B1→B2→C1) daemon thread-এ; status লেখা; try/except বাধ্যতামূলক।
- **U1c** — voiceover/render background (`/voiceover/{job_id}/auto_tts`,
  `/final/{job_id}`), polling page, resumable clip-reuse (একই clip আবার TTS-এ
  খরচ হয় না)।
- **U2a** — `pipeline/gemini_rotation.py` (নতুন): `classify_error()`
  (মার্কার + `NonRotatableError` instance, safe-default rotatable),
  `CallBudget` (`None` = unlimited), `call_with_rotation_v2()` round-robin +
  attempts লগ + `AllKeysExhausted`/`CallBudgetExceeded`। Exception শ্রেণি:
  `GeminiRotationError` → `NonRotatableError` → `ContentSafetyBlocked`।
- **U2b** — ৩টা কল-সাইটে (subtitle_extract/translator/voiceover_auto)
  `call_with_rotation_v2` + shared per-job `CallBudget` wire করা; same-key
  429-backoff বদলে সাথে সাথে rotate (429-ও rotatable), non-rotatable
  (400/invalid/content/safety/blocked) এ প্রথম key-তেই থামা।
- **U3a** — translator-এ **batch-split auto-repair**: `_translate_chunk` /
  `_repair_split` (depth-bounded), শুধু সত্যিই ব্যর্থ লাইনগুলো
  `translation_fallback: true` পায়, প্রতিবেশী অনূদিত থাকে।
- **U3b** — auto-TTS-এ **failed-serial দ্বিতীয় বাউন্ডেড পাস**: শুধু
  failed serial-গুলো একবার retry, rotation state + একই CallBudget carry
  করে, সফল হলে silence-প্লেসহোল্ডার real audio দিয়ে replace + timestamps
  recalc (E1 সঠিক speed-ratio পায়)।
- **U4** — **per-job persistent logs**: `pipeline/job_logging.py`
  `get_job_logger(job_id)` → সব Gemini/ffmpeg stage-এর progress
  `uploads/<job_id>/logs/pipeline.log`-এ (append, handler-dedup, key কখনো
  raw লগ হয় না)।
- **U5** — **pre-flight offline dry-run gate**:
  `python3 -m pipeline.dry_run_check --job-id <job_id> [--upload-root ...]`
  — network/ffmpeg/Gemini ছাড়া একই job-এর JSON ফাইলগুলোর নিজস্ব সংগতি
  যাচাই (B2 serial gap/duplicate + required keys, C1 count/order ম্যাচ +
  fallback %, D4 count ম্যাচ + end>start + no overlap, E1 flagged
  distribution — তথ্যমূলক)। Exit 0 = কোনো ব্লকিং error নেই, 1 = ব্লকিং
  error (missing ফাইল ব্লকিং নয় — শুধু skip)। app.py-তে নতুন এন্ডপয়েন্ট
  যোগ করা হয়নি — শুধু standalone CLI (ভবিষ্যতে ইচ্ছা হলে UI-তে আনার জন্য
  আলাদা ফলো-আপ)।

## পরের কাজ

প্ল্যান সম্পূর্ণ — কোনো বাধ্যতামূলক পরের চাংক নেই। ভবিষ্যতের প্রস্তাবিত
ফলো-আপ (Scope-এর বাইরে, আলাদা রিকোয়েস্ট):
- BGM preservation (মূল ভিডিওর BGM ডাক করা / music bed) — ব্যবহারকারীর
  বন্ধুর কাছ থেকে confirm করে আলাদা ফিচার প্ল্যান হিসেবে।
- চূড়ান্ত Hindi TTS ভয়েস/পার্সোনা নির্বাচন (`config.py`-তে
  `TTS_VOICE_HINDI` placeholder — creative decision, বাস্তব আউটপুট শুনে)।
- প্রোডাকশন concern (job queue, স্টোরেজ ক্লিনআপ, concurrency) — স্থানীয়
  ব্যবহারের বাইরে গেলে।
- dry_run_check-কে UI-তে (রিট্রাই বাটনের পাশে) আনা।

বিস্তারিত data-flow/চলার নিয়ম: `docs/FINAL_SUMMARY.md`। চূড়ান্ত চ্যাঞ্জলগ:
`docs/CHANGELOG.md`।
