# HANDOFF NEXT

## প্রজেক্ট: manhwa-video-dubber-v6 (Full-Auto Pipeline)

এই রিপো `manhwa-video-dubber`-এর version-bump কপি (পুরনো রিপো:
https://github.com/shafin262619-jpg/manhwa-video-dubber — অক্ষত রাখা
হয়েছে)। পুরো commit history + পুরনো ট্যাগ
(`manhwa-video-dubber-v6-robustness-final` ইত্যাদি) এখানে প্রিজার্ভ
করা আছে।

এই রিপোতে **Full-Auto Pipeline** প্ল্যান সম্পূর্ণ হয়েছে — ধারাবাহিক ১২টা
চাংক FA-A1 → FA-B1 → FA-B2 → FA-B3 → FA-C1 → FA-C2 → FA-D1 → FA-D2 →
FA-E1 → FA-E2 → FA-F1 → FA-F2। প্রতিটা চাংকের বিস্তারিত প্রম্পট:
`docs/FA_CHUNK_BATCH.md`। প্রতিটা চাংক শেষে `chunk-FA-<id>-done` ট্যাগ
বসানো হয়েছে।

## স্ট্যাটাস: সব FA চাংক সম্পূর্ণ (A1-F2) — পুরো Full-Auto Pipeline বাস্তবায়িত

**সব চাংক-ট্যাগ (`chunk-FA-*`) বসানো হয়েছে, শেষে
`manhwa-video-dubber-v6-full-auto-final`।** গত সেশন FA-D2-এর ঠিক পরে
থেমেছিল (FA-E1-এর মাঝখানে, docs আপডেটের সময়) — এই সেশনে tag history থেকে
নিশ্চিত হয়ে FA-E1 শুরু থেকে সম্পূর্ণ করেছি, তারপর FA-E2 → FA-F1 → FA-F2
একে একে শেষ করেছি। পুরো স্যুট এখন **২৯০টা টেস্ট OK**।

### FA-E1 (ব্যাকওয়ার্ড-কম্প্যাট অডিট) — checklist ফলাফল

1. **Manual voice-source override resumable/idempotent** — ✓ (বাগ পাওয়া
   ও ফিক্স করা হয়েছে)। আগের কোডে `upload_status_page()` flat
   `stage`/`state` ফিল্ডে গেট করত, তাই override-এর পরে দুইটা infinite
   redirect-loop কেস ছিল: (ক) auto_tts শেষ → `auto_full_render`/`done`
   অবস্থায় override করে user_upload করলে `/upload` পেজ `upload_pipeline`
   পোল করে চিরকাল loop করত; (খ) user_upload শেষ → override করে auto_tts
   করলে `/upload` এমন একটা stage পোল করত যা শুরুই হয়নি। ফিক্স:
   - user_upload ব্র্যাঞ্চ এখন stage-history-এ `upload_pipeline` done
     কিনা দেখে (flat fields না), ফলে override-এর পর সরাসরি audio-upload
     ফর্ম দেখায়।
   - auto_tts ব্র্যাঞ্চে `auto_full_render` stage absent + `upload_pipeline`
     done হলে পেজ নিজেই `_start_stage(job_id, "auto_full_render",
     _run_auto_full_render)` দিয়ে চেইন resume করে — infinite loop-এর
     বদলে ফাইনাল ভিডিওতে converge হয়।
   - FA-C1-এর inline same-thread লজিকটা `_run_auto_full_render()`-এ
     আলাদা করে নেওয়া হয়েছে (একই থ্রেডে, নতুন thread spawn না — FA-C1
     hard constraint অক্ষত)।
2. **পুরনো ম্যানুয়াল রুটগুলো সরাসরি URL-এ কাজ করে** — ✓। `GET/POST
   /voiceover/{job_id}/choose`, `GET /voiceover/{job_id}/align_uploaded`,
   `GET /voiceover/{job_id}/auto_tts`, `GET /review/{job_id}`, `POST
   /review/{job_id}/edit`, `GET /final/{job_id}`, `GET /download/{job_id}`
   — existing G1 (`test_app_orchestration.py`) + `test_voiceover_upload` +
   `test_voiceover_auto` + `test_render_final` + `test_review` টেস্টে
   এখনো পাশ করে; নতুন `test_backward_compat_audit.py`-তেও পুরনো flow
   সরাসরি URL দিয়ে চালিয়ে যাচাই করা আছে।
3. **পুরো existing test suite (U0-U5) অপরিবর্তিত** — ✓, ২৮৪টা পুরনো
   টেস্ট কোনো বদল ছাড়াই পাশ করে।
4. **`pipeline/dry_run_check.py` (U5) নতুন flow-এর সাথেও কাজ করে** — ✓।
   আর্টিফ্যাক্ট নাম/লোকেশন বদলায়নি, তাই একটি FA-D4/E1 আর্টিফ্যাক্টসহ
   fixture job-এ `python3 -m pipeline.dry_run_check --job-id <id>`
   exit 0 দেয়; existing `test_dry_run_check.py` পাশ করে।

নতুন regression টেস্ট: `pipeline/tests/test_backward_compat_audit.py`
(+৪টা HTTP টেস্ট — override-এর প্রতিটা কেস + পুরনো routes direct-URL
flow)।

### FA-E2 (permanent E2E regression টেস্ট)

নতুন `pipeline/tests/test_full_auto_orchestration.py` (G1-এর মতোই
HTTP-only, mocked Gemini/ffmpeg, D2 real-ffmpeg-silence প্যাটার্নে) —
দুটো permanent end-to-end টেস্ট:

- `test_auto_tts_zero_click_end_to_end` — POST /upload (auto_tts) → শুধু
  `GET /api/jobs/{id}/status` পোল (অন্য কোনো endpoint কল না) → stage-through
  `upload_pipeline → auto_full_render` যাচাই → `GET /download/{job_id}`
  ফাইনাল ভিডিও ফেরায়। `/choose`, `/align_uploaded`, `/final` — কোনোটাই
  কল হয়নি (recording client দিয়ে যাচাই করা)।
- `test_user_upload_single_pause_end_to_end` — POST /upload (user_upload) →
  `upload_pipeline` done-এই থামে (auto-continue না, final_video নেই) → POST
  `/voiceover/{id}/upload` (fake wav) → `user_audio_pipeline` done → final
  video downloadable। `/choose`, `/align_uploaded`, `/final` — কোনোটাই কল
  হয়নি (single-pause claim-এর প্রমাণ)।
- Hard constraint মেনে চলা হয়েছে: পুরনো `test_app_orchestration.py` এক
  লাইনও বদলায়নি, এখনো আলাদাভাবে পাশ করে (ব্যাকওয়ার্ড-কম্প্যাট প্রমাণ)।

## স্ট্যাটাস: সব FA চাংক (A1-F2) সম্পূর্ণ — PRD-এর মূল requirement বাস্তবায়িত ও রিগ্রেশন-টেস্টেড

**`manhwa-video-dubber-v6-full-auto-final` ট্যাগ বসানো হয়েছে।**

সব ১২টা Full-Auto Pipeline চাংক (FA-A1 → FA-B1 → FA-B2 → FA-B3 → FA-C1 →
FA-C2 → FA-D1 → FA-D2 → FA-E1 → FA-E2 → FA-F1 → FA-F2) সম্পূর্ণ, commit +
push + tag করা হয়েছে। চূড়ান্ত টেস্ট: **২৯০টা, ১০০% পাস**।

PRD-এর মূল requirement বাস্তবায়িত:
- **auto_tts পাথ** — upload করলে zero-click ফাইনাল ভিডিও পর্যন্ত চলে।
- **user_upload পাথ** — শুধু অডিও-আপলোডেই থামে, তারপর zero-click ফাইনাল
  ভিডিও পর্যন্ত।
- পুরনো ম্যানুয়াল রুটগুলো অক্ষত (FA-E1 অডিটে যাচাই)।

বিস্তারিত: `docs/FA_CHUNK_BATCH.md`, `docs/FINAL_SUMMARY.md`-এর নতুন
"## Full-Auto Pipeline (FA1-F2)" সেকশন, `docs/CHANGELOG.md`।

**বাকি শুধু ব্যবহারকারীর নিজের real-media QA রান** — `docs/FINAL_SUMMARY.md`-এর
"Full-Auto Pipeline (FA1-F2)" সেকশনের নোট অনুযায়ী তিনটা point:
(ক) real Gemini key + real ভিডিও দিয়ে auto_tts পাথে সত্যিই কোনো ক্লিক ছাড়াই
ফাইনাল ভিডিও আসে কিনা browser-এ নিজের চোখে যাচাই;
(খ) user_upload পাথে সত্যিই শুধু অডিও-আপলোডেই থামে, এরপর আর কোনো ক্লিক লাগে
না কিনা যাচাই;
(গ) ফাইনাল ভিডিওর কোয়ালিটি U-সিরিজের আগের আউটপুটের মতোই আছে (এই আপডেট
UX/wiring বদলেছে, pipeline-এর আউটপুট কোয়ালিটি বদলায়নি) — স্পট-চেক করে
কনফার্ম।
