# HANDOFF NEXT

গ্রুপ A-E (subtitle-QA-fix, A1-E4) + E5 (real-media QA-writeback fix) +
E6 (draft duration-validation fix) + E7 (cascade-crash fix) +
E8 (user_upload duration-check removal) + E9 (duration-drift fix) + E10
(test-isolation fix) + F8 (Whisper timing authority) + F9 (per-job config,
3-job history + confirm eviction, resume-from-interruption) + F10 (progress
bar / log panel / history-tab UI) + F11 (Bengali error text) সম্পূর্ণ।

F10/F11 (progress bar + log panel + history tab UI + Bengali errors):

- **`pipeline/stages.py` (নতুন)** — progress bar-এর single source of truth:
  `STAGE_SEQUENCE` (F1_extract → C1_translate → D2_voiceover_or_D3_align →
  D4_unify → E1_guideline → E2_draft → F3_final), `STAGE_LABELS_BN` (বাংলা
  label), `STAGE_KEY_GROUPS` (প্রতিটা slot-এর status-file stage key — D2/D3 এক
  slot শেয়ার করে), `UMBRELLA_TO_SEQUENCE` (`final_render` → F3 slot)।
- **Progress bar (F10.1)** — `_polling_page`-এর spinner-এর জায়গায়
  `.progress-track`/`.progress-fill` বার (width = done stages + in-stage
  fraction, fraction = `extra.progress.processed/total`, না থাকলে 0.5),
  নিচে ৭টা stage row (✓/spinner/✗/○ + বাংলা label)। সব বিদ্যমান ২-সেকেন্ড
  `poll()` লুপের ভেতরেই, কোনো framework/CDN ছাড়াই। "Processing…" banner টেক্সট
  রাখা হয়েছে (পুরনো টেস্ট ভাঙবে না)।
- **Live log panel + `GET /api/jobs/{job_id}/logs` (F10.2)** — endpoint কখনো
  raise করে না (ফাইল নেই → `{"lines": [], "next_line": 0}`), negative /
  past-end `since_line` clamp করে, নতুন লাইন + `next_line` দেয়। Polling
  page-এ fixed-bottom docked panel (~30vh, monospace dark), প্রতি ৩ সেকেন্ডে
  poll, নতুন লাইন append, শুধু bottom-এ থাকলে auto-scroll।
- **History page + nav (F10.3)** — `site_header`-এ "ইতিহাস" nav link; `GET
  /history` এখন HTML page (card: job_id, created_at, target_lang,
  voice_source, colored done/running/error badge; "দেখুন" → `/review/{job_id}`;
  "রিজিউম করুন" → POST `/jobs/{job_id}/resume` → `/resume/{job_id}` polling →
  `/final/{job_id}`)। Resume button error job-এ + stale-running job-এ (10+
  মিনিট status update নেই, status-file mtime থেকে)। Machine-readable JSON
  `/api/history`-তে স্থানান্তরিত (test_f9_endpoints-এর `/history` JSON
  assertion-গুলো `/api/history`-তে retarget করা হয়েছে)।
- **Bengali history-full confirm (F10.4)** — upload-form JS-এ 409
  `needs_confirm`-এ বাংলা `confirm()` dialog: OK → `confirm-start`?
  `delete_files=true`, Cancel → `delete_files=false`, দুটোতেই polling page-এ
  চলে যায়।
- **`pipeline/error_bn.py` + `detail_bn` (F11)** — `explain_bn(exc, stage)`
  `_friendly_error`-এর mirror: CallBudgetExceeded, AllKeysExhausted (empty +
  populated), ffmpeg/ffprobe/subprocess failure, whisper import/runtime error,
  malformed-transcript placeholder (F12), timeout/network; fallback generic —
  stage-এর বাংলা label + truncated English text। কখনো raise করে না। সব ১১টা
  app error site এখন `_write_error_status` দিয়ে `detail` + `detail_bn` দুইটাই
  লেখে; error banner-এ `detail_bn` primary, English `detail` "বিস্তারিত
  (English)" toggle-এর পেছনে।
- **Tests**: full suite **৫০২ টেস্ট OK** (was ৪৭১; +৩১) —
  test_error_bn.py (per-exception case + never-raises), test_f10_endpoints.py
  (logs incremental `since_line` + clamping, /history HTML 0/1/3 job, badge +
  resume button incl. stale-running, keep-files confirm, error-path
  `detail_bn`) নতুন; test_f9_endpoints.py (/api/history), test_job_status.py
  (detail/detail_bn একসাথে persist) বাড়ানো হয়েছে।

বাকি কাজ:
- F9-এর ব্রাউজার-side `confirm()` eviction flow (409 → dialog → confirm-start)
  এখন বাংলা dialog-এ রূপান্তরিত — রিয়েল ব্রাউজারে ম্যানুয়াল verify বাকি।
- ব্যবহারকারীর নিজের real-media QA রান:
  - docs/FINAL_SUMMARY.md → "Subtitle QA Fixes (A1-E4)" → "The user must do this"
  - Whisper-primary পাথ (যখন Whisper ইনস্টল করা থাকবে) দিয়ে একটা job রি-রান
    করে confirm করো যে subtitles-এর টাইমিং Whisper-এর সাথে মিলছে, `text_source`
    ফিল্ড ঠিকমতো আসছে, এবং `max(end_sec)` কোনো ফাইলে probed audio-দৈর্ঘ্যের
    বাইরে যাচ্ছে না।
- Whisper এখনো এনভায়রনমেন্টে ইনস্টল নেই (pip `openai-whisper` + `numpy`) —
  ইনস্টল করলে F1-F3/D3-এর প্রাইমারি পাথ সক্রিয় হবে; না করলে pure-Gemini
  fallback-এ সব আগের মতোই চলে (সম্পূর্ণ resilience সংরক্ষিত)।
- E6-এর `expected_duration_sec` (draft ≈ source duration) ধারণাটা এখন শুধু
  auto-TTS পাথেই প্রযোজ্য; `user_upload` পাথে draft-এর মোট দৈর্ঘ্য source-এর
  চেয়ে আলাদা হওয়াই প্রত্যাশিত (কিন্তু ভয়েসওভার অডিও দৈর্ঘ্যের সমান থাকবে)।
- **F12 (transcript upload / language dropdown / auto-detect) শুরু হয়নি** —
  `error_bn`-এ malformed-transcript placeholder আছে মাত্র; upload form-এ
  transcript upload, language dropdown, auto-detect এখনো নেই।
- **F13 (retry-with-escalation UI) শুরু হয়নি** — "আবার চেষ্টা করুন" লিঙ্কটাই
  এখনো একমাত্র retry পাথ; escalation (fresh budget / more keys / smaller
  model) সহ retry UI নেই।
