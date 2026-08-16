# HANDOFF NEXT

গ্রুপ A-E (subtitle-QA-fix, A1-E4) + E5 (real-media QA-writeback fix) +
E6 (draft duration-validation fix) + E7 (cascade-crash fix) +
E8 (user_upload duration-check removal) + E9 (duration-drift fix) + E10
(test-isolation fix) + F8 (Whisper timing authority) + F9 (per-job config,
3-job history + confirm eviction, resume-from-interruption) সম্পূর্ণ।

F9 (`per-job config + history + resume`): job_config, 3-job history, resume

- **`pipeline/job_config.py` (নতুন)** — প্রতিটা job-এর জন্য
  `uploads/<job_id>/job_config.json` creation-এর সময় একবার লেখা হয়, কোনো
  Gemini/Whisper call-এর আগেই। লেখা থাকে: engine (`whisper_primary` /
  `gemini_only`), target language (`target_lang`, আজ `"hi"`; `source_lang`
  শুধু schema — F12-এ auto-detect), voice source। `write_config` engine /
  voice source validate করে (ValueError); `read_config` কখনো raise করে না —
  pre-F9 job (dir আছে, config নেই) → F9 defaults, missing job dir → `None`।
  `default_engine()` = Whisper import করা গেলে `whisper_primary`, নাহলে
  `gemini_only`। Upload ফর্মের engine radio `default_engine()` থেকে
  pre-selected।
- **Engine-gated Whisper (F9 §2)** — `whisper_align.engine_allows_whisper()`
  job-এর config পড়ে: `gemini_only` job Whisper skip করে **যদিও** Whisper
  installed থাকে (ফোন/Termux user-দের জন্য, যারা ভারী torch/whisper install
  এড়াতে চায়)। pre-F9 job (config ফাইল নেই) F8 আচরণ রাখে (Whisper allowed)।
  Gated call site: `subtitle_extract._whisper_merge` এবং
  `voiceover_upload.align_uploaded_voiceover`।
- **Per-stage status + progress** — `job_status.run_stage()` `running` →
  stage চলে → `done` (stage নিজে লেখা `progress` dict সংরক্ষিত) বা `error` +
  re-raise (status লেখা best-effort)। `full_auto_chain`-এর প্রতিটা stage
  wrapped, তাই প্রতিটা stage-এর আলাদা status entry থাকে
  (D2_voiceover / D3_align / D4_unify / E1_guideline / E2_draft / F3_final)
  ক্রম অনুযায়ী। `subtitle_extract.extract_subtitles` ও
  `auto_cut.build_draft_video` optional `progress_cb(processed, total)` নেয়।
  App-এর `_run_upload_pipeline` F1_extract ও C1_translate `run_stage` দিয়ে
  চালায় (F1-এ per-chunk progress)।
- **Resume-from-interruption (F9 §5)** — নতুন `pipeline/resume.py`:
  `find_resume_point(job_id)` আর্টিফ্যাক্ট থেকে পরের stage বের করে
  (`subtitles_hi.json` নেই → `"upload_pipeline"`, তারপর প্রথম missing:
  timestamps → edit_guideline → draft_final_video.mp4 →
  `outputs/<job_id>/final_video.mp4`; সম্পূর্ণ হলে `None`)।
  `resume_job` `start_from` দিয়ে chain চালায় (আগের stage skipped, result key
  `None` — সম্পন্ন stage কখনো re-run হয় না), সম্পূর্ণ/আপলোড-অসম্পূর্ণ job-এ
  `RuntimeError`। নতুন endpoint `POST /jobs/{job_id}/resume` →
  `{"resume_point", "status": "processing"}`, background thread-এ `_run_resume`
  চলে, `resume` stage status persist হয়; কিছু resume করার না থাকলে 409।
  Acceptance test: যে stage নিজের artifact লিখে তারপর "crash" হয়েছে (raise),
  resume-এ সেটা **আবার চলে না** (D3 `call_count` 1-ই থাকে)।
- **3-job history + confirm-based eviction (F9 §4)** — নতুন
  `pipeline/history_store.py`: index `uploads/_history_index.json`, cap
  `HISTORY_LIMIT` (3)। `register_job` newest-first যোগ করে; index full হলে
  **evict করে না** — `{"added": False, "would_evict": <oldest>,
  "needs_confirm": True}` ফেরত দেয়। `evict_job(job_id, delete_files=...)`
  drop করে, চাইলে ফাইল-ও মুছে — শুধু user confirm-এর পরে।
  `list_history` newest-first, live metadata (job_config, voice source,
  status, target video name) সহ, missing dir skip। Endpoint `GET /history` →
  `{"history": [...], "limit": 3}`।
- **Browser `confirm()` eviction flow** — upload ফর্মে, `POST /upload`-এর 409
  (`needs_confirm`) এ `window.confirm()` dialog-এ oldest job-এর নাম দেখিয়ে
  জিজ্ঞেস করে; accept করলে `POST /jobs/{job_id}/confirm-start?evict_job_id=
  <oldest>&delete_files=true` → evict + register + pipeline start; decline
  করলে কিছুই evict হয় না। `POST /upload` এখন engine/target_lang form field-ও
  নেয় (validated) এবং কোনো Gemini/Whisper কাজের আগেই `job_config.json`
  লেখে।
- **Tests**: full suite **৪৭১ টেস্ট OK** (was ৪৩০; +৪১) —
  test_job_config.py, test_history_store.py, test_resume.py,
  test_f9_endpoints.py (HTTP: 409 confirm flow, confirm-start, /history,
  resume endpoint) নতুন; test_full_auto_chain.py (per-stage status order),
  test_subtitle_extract.py + test_voiceover_upload.py (engine gating —
  `gemini_only` কখনো transcribe করে না) বাড়ানো হয়েছে।

বাকি কাজ:
- **F10 (progress bar / log panel / history-tab UI) শুরু হয়নি** — `progress_cb`,
  per-stage status data, `GET /history`-এর ডেটা সবই server-side তৈরি আছে,
  কিন্তু ব্রাউজার-side UI (এনিমেটেড progress bar + polling, log panel +
  auto-scroll, history tab) একটাও নেই। F10-এর আগে ব্রাউজারে ম্যানুয়াল
  verify লাগবে।
- **F11 (Bengali error text) শুরু হয়নি** — error-গুলো এখনো ইংরেজি;
  Bengali `NO_ACTIVE_KEY_MESSAGE`-এর মতো user-facing error text localization
  এখনো করা হয়নি। F9-এর `confirm()` dialog-ও এখনো ইংরেজি text-এ।
- F9-এর ব্রাউজার-side `confirm()` eviction flow (409 → dialog → confirm-start)
  শুধু HTTP-level টেস্টে (test_f9_endpoints.py) আচ্ছাদিত — রিয়েল ব্রাউজারে
  ম্যানুয়াল verify বাকি।
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
