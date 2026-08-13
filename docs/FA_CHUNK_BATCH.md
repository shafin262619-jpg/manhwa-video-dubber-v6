নিচে একাধিক চাংক-প্রম্পট আছে, ক্রমানুসারে (FA-A1 থেকে FA-F2, মোট ১২টা)।

প্রথমে repo-র সর্বশেষ tag/commit আর docs/HANDOFF_NEXT.md চেক করে দেখো
ইতিমধ্যে কোন চাংক পর্যন্ত শেষ হয়ে গেছে — যেগুলো আগেই chunk-<id>-done
ট্যাগসহ শেষ, সেগুলো আবার করার দরকার নেই। এরপর যেখানে বাকি আছে সেখান
থেকে শুরু করে, প্রতিটা চাংক তার নিজের 'Definition of Done' (টেস্ট,
HANDOFF_NEXT.md/CHANGELOG.md আপডেট, commit, push, tag) সম্পূর্ণ করে
নিজে থেকেই পরের চাংকে চলে যাও — প্রতি চাংকের পর ব্যবহারকারীর
কনফার্মেশনের জন্য থামার দরকার নেই। context/token ফুরিয়ে গেলে যে চাংকে
আছ সেটার নিজের নিয়ম অনুযায়ী HANDOFF_NEXT.md-এ ঠিক কতটুকু হলো/কতটুকু
বাকি স্পষ্ট লিখে commit+push করে রেখো, যাতে অন্য একটা ফ্রেশ সেশন এখান
থেকেই চালিয়ে যেতে পারে।

নোট: এই প্ল্যানের রিপো-রেফারেন্স মূলত পুরনো manhwa-video-dubber রিপোর
নামে লেখা ছিল, কিন্তু এই সেশনে কাজটা আসলে নতুন version-bump রিপো
manhwa-video-dubber-v6-এ হবে (নিচের প্রতিটা চাংকে URL/ট্যাগ ইতিমধ্যে
আপডেট করে দেওয়া হয়েছে)।

--- CHUNK FA-A1 ---

আমি manhwa-video-dubber-এর "Full-Auto Pipeline" আপডেটে কাজ করছি, একটা
GitHub-ভিত্তিক multi-step handoff chain-এর "চাংক FA-A1" (মোট ১২টার
প্রথমটা)।

Repo: https://github.com/shafin262619-jpg/manhwa-video-dubber-v6.git
(branch: main)

প্রথমে করো:
1. Repo clone/pull করো (tag manhwa-video-dubber-v6-robustness-final থেকে
   verify করো)।
2. docs/HANDOFF_NEXT.md পুরোটা পড়ো।
3. docs/GITHUB_AGENT_HANDOFF_PLAN_FULL_AUTO.md-এর "Bird's-eye ম্যাপিং"
   আর এই A1 সেকশন পড়ো (পুরো প্ল্যানের প্রথম চাংক এইটাই)।
4. app.py-র home(), upload_video(), আর voiceover_unify.py-র
   set_voice_source()/ALLOWED_MODES পড়ে বোঝো — এই চাংক এগুলোর ওপরই কাজ
   করবে।

লক্ষ্য (কেন এই চাংক): এখন home page-এ শুধু ভিডিও আপলোড হয়, voice-source
(auto TTS নাকি নিজের অডিও) বাছাইটা upload শেষ হওয়ার *পরে* আলাদা পেজে
(`/voiceover/{job_id}/choose`) গিয়ে করতে হয় — এটাই এই আপডেটের প্রথম গ্যাপ।
এই চাংকের কাজ: সেই প্রশ্নটা upload ফর্মেই upfront নিয়ে নেওয়া, যাতে পরের
গ্রুপগুলো (C, D) পুরো বাকি চেইন zero-click চালাতে পারে।

তোমার স্কোপ:

1. app.py-র `home()`-এর upload ফর্মে একটা radio-group যোগ করো:
   - "সিস্টেম নিজেই ভয়েসওভার বানাক (Gemini TTS)" — value="auto_tts",
     **ডিফল্ট checked**।
   - "আমি নিজের/অন্য AI দিয়ে বানানো অডিও দেব" — value="user_upload"।
   ফর্মের existing inline `<script>`-এ FormData-তে এই ফিল্ডটাও যোগ করো
   (ইতিমধ্যে ফাইল যোগ হচ্ছে সেভাবেই)।

2. `upload_video()` endpoint-এ নতুন optional param নাও:
   `voice_source: str = Form("auto_tts")`
   - `voiceover_unify.ALLOWED_MODES`-এর মধ্যে না থাকলে 400 error (existing
     `InvalidVoiceSourceError` প্যাটার্নে)।
   - Valid হলে, upload সফল হওয়ার সাথে সাথেই (B1→B2→C1 background thread
     শুরু করার আগে বা তার ঠিক শুরুতে) `voiceover_unify.set_voice_source(
     job_id, voice_source)` কল করে সংরক্ষণ করো — এখন থেকে এই choice সবসময়
     upload-এর মুহূর্তেই জানা থাকবে, পরের কোনো গ্রুপকে (C/D) আর এটার জন্য
     আলাদা এন্ডপয়েন্ট/ক্লিকের অপেক্ষা করতে হবে না।

Hard constraint: এই চাংকে `_run_upload_pipeline`, `voiceover_choose`,
`/voiceover/{job_id}/choose` route — এসবের **আচরণ বদলাবে না**, শুধু
voice_source-টা এখন *আগে থেকেও* সেভ থাকবে। পুরনো `/voiceover/{job_id}/
choose` পেজ দিয়ে ম্যানুয়ালি আবার বদলানো এখনো কাজ করবে (এটা backward-compat
হিসেবে দরকার, গ্রুপ E-তে ভেরিফাই হবে)।

টেস্ট: pipeline/tests/-এ existing HTTP TestClient প্যাটার্নে যোগ করো —
(ক) POST /upload-এ voice_source="user_upload" পাঠিয়ে verify করো
voice_source_choice.json সাথে সাথেই লেখা হয়েছে (upload_pipeline background
thread শেষ হওয়ার অপেক্ষা ছাড়াই)। (খ) voice_source না পাঠালে ডিফল্ট
"auto_tts" সেট হয়। (গ) invalid value ("garbage") দিলে 400।

শেষে (Definition of Done):
1. `python3 -m unittest discover -s pipeline/tests -v` — সব পুরনো টেস্ট
   (২৭০+) + নতুন টেস্ট পাশ করছে কনফার্ম করো।
2. `python3 -m py_compile app.py`।
3. docs/HANDOFF_NEXT.md ওভাররাইট: "FA-A1 (upfront voice-source input)
   সম্পূর্ণ। পরের কাজ: FA-B1 (auto_tts orchestration wrapper, পুরো
   D2→D4→E1→E2→F3 চেইন এক ফাংশনে, এখনো routes-এ wire না)।"
4. docs/CHANGELOG.md-এ এন্ট্রি যোগ করো (existing U-এন্ট্রিগুলোর নিচে)।
5. Commit: "chunk FA-A1: upfront voice-source input on /upload" — push করো।
6. Tag: git tag chunk-FA-A1-done && git push origin chunk-FA-A1-done

কাজ অসম্পূর্ণ থাকলেও ৩-৬ ধাপ করো, HANDOFF_NEXT.md-এ কী বাকি স্পষ্ট লেখো।

--- CHUNK FA-B1 ---

আমি manhwa-video-dubber-এর Full-Auto Pipeline আপডেটে কাজ করছি, "চাংক
FA-B1" (FA-A1 শেষ)।

Repo: https://github.com/shafin262619-jpg/manhwa-video-dubber-v6.git
(branch: main)

প্রথমে করো:
1. Repo clone/pull করো (tag chunk-FA-A1-done থেকে verify করো)।
2. docs/HANDOFF_NEXT.md পড়ো।
3. app.py-র `_process_auto_tts()` আর `_continue_from_voiceover()` পড়ো —
   এই দুটোই D2→D4→E1→E2 চেইন করে, এই চাংক তারই ওপর **F3** যোগ করে একটা
   standalone ফাংশনে বের করে আনবে।
4. pipeline/render_final.py-র `finalize_video(job_id)` সিগনেচার পড়ো।

লক্ষ্য: বর্তমানে F3 (final render) শুধু `/final/{job_id}` পেজে ম্যানুয়ালি
গেলেই চলে (গ্যাপ G3, সবচেয়ে বড় গ্যাপ)। এই চাংকের কাজ শুধু একটা pure-Python
ফাংশন বানানো যেটা পুরো auto-TTS চেইন **F3 সহ** এক জায়গায় চালাবে — HTTP
route-এ wire করা এখনো না (সেটা গ্রুপ C)।

তোমার স্কোপ:

নতুন ফাইল `pipeline/full_auto_chain.py` বানাও, তাতে:

    def run_auto_tts_chain(job_id, call_budget=None):
        """D2 (auto TTS) -> D4 (unify) -> E1 (edit guideline)
        -> E2 (draft render) -> F3 (final render).

        ব্যর্থ হলে exception raise করে (uncaught না ধরে বরং caller-এর
        জন্য propagate করে) — app.py-র existing _friendly_error/
        job_status প্যাটার্নে ধরার জন্য।
        """

ভেতরে ঠিক এই সিকোয়েন্স কল করো:
1. `voiceover_auto.generate_auto_voiceover(job_id, call_budget=call_budget)`
2. `voiceover_unify.unify_voiceover_timestamps(job_id)`
3. `edit_guideline.build_edit_guideline(job_id)`
4. `auto_cut.build_draft_video(job_id)`
5. `render_final.finalize_video(job_id)`  ← **নতুন অংশ, আগে এটা এই চেইনে
   ছিল না**

রিটার্ন: একটা dict যাতে D2-এর result + F3-এর result দুটোই আছে (যেমন
`{"voiceover": <D2 result>, "final": <F3 result>}`), যাতে caller (গ্রুপ
C) status-এ পুরোটা লিখতে পারে।

Hard constraint: এই চাংকে **app.py স্পর্শ করবে না** — `_process_auto_tts`/
`_continue_from_voiceover` এখনো আগের মতোই থাকবে (গ্রুপ E-তে backward-compat
হিসেবে দরকার)। এই নতুন ফাংশন শুধু আলাদা ফাইলে থাকবে, এখনো কোথাও কল হবে না।

টেস্ট: নতুন `pipeline/tests/test_full_auto_chain.py` — existing D2/E2/F3
টেস্টগুলোর মতোই Gemini TTS + ffmpeg mock করে (existing মকিং প্যাটার্ন
অনুসরণ করো), `run_auto_tts_chain(job_id)` সরাসরি কল করো (কোনো HTTP না),
আর assert করো `outputs/<job_id>/final_video.mp4` তৈরি হয়েছে।

শেষে (Definition of Done):
1. পুরো test suite পাশ করছে কনফার্ম করো।
2. `python3 -m py_compile pipeline/full_auto_chain.py`।
3. docs/HANDOFF_NEXT.md: "FA-B1 (auto_tts orchestration wrapper, F3 সহ)
   সম্পূর্ণ, standalone, এখনো app.py-তে wire হয়নি। পরের কাজ: FA-B2
   (user_upload-এর জন্য একই প্যাটার্নের wrapper)।"
4. docs/CHANGELOG.md-এ এন্ট্রি যোগ করো।
5. Commit: "chunk FA-B1: run_auto_tts_chain() — D2 to F3 in one function"
   — push করো।
6. Tag: git tag chunk-FA-B1-done && git push origin chunk-FA-B1-done

--- CHUNK FA-B2 ---

আমি manhwa-video-dubber-এর Full-Auto Pipeline আপডেটে কাজ করছি, "চাংক
FA-B2" (FA-B1 শেষ)।

Repo: https://github.com/shafin262619-jpg/manhwa-video-dubber-v6.git
(branch: main)

প্রথমে করো:
1. Repo clone/pull করো (tag chunk-FA-B1-done থেকে verify করো)।
2. docs/HANDOFF_NEXT.md পড়ো।
3. pipeline/full_auto_chain.py (FA-B1) পড়ো — এই চাংক একই স্টাইলে দ্বিতীয়
   ফাংশন যোগ করবে।
4. pipeline/voiceover_upload.py-র `align_uploaded_voiceover(job_id)` (D3)
   আর app.py-র `align_uploaded_page()` পড়ে বোঝো এখন এটা কীভাবে ম্যানুয়ালি
   ট্রিগার হয়।

লক্ষ্য: নিজের-অডিও পাথে ঠিক একবার থামা উচিত — অডিও নেওয়ার জন্য। এখন সেটার
পরেও আরও কয়েকটা ম্যানুয়াল ক্লিক লাগে (align, তারপর final)। এই চাংক অডিও
সেভ হয়ে যাওয়ার *পরের* পুরো চেইনটা এক ফাংশনে বানাবে (গ্রুপ D পরে এটা রুটে
wire করবে)।

তোমার স্কোপ:

pipeline/full_auto_chain.py-তে দ্বিতীয় ফাংশন যোগ করো:

    def run_user_upload_chain(job_id):
        """(precondition: voiceover_hi.wav ইতিমধ্যে সেভ করা আছে,
        voiceover_upload.save_uploaded_voiceover() দিয়ে)
        D3 (align) -> D4 (unify) -> E1 (edit guideline)
        -> E2 (draft render) -> F3 (final render)."""

সিকোয়েন্স:
1. `voiceover_upload.align_uploaded_voiceover(job_id)`  (D3, existing)
2. `voiceover_unify.unify_voiceover_timestamps(job_id)`
3. `edit_guideline.build_edit_guideline(job_id)`
4. `auto_cut.build_draft_video(job_id)`
5. `render_final.finalize_video(job_id)`

রিটার্ন: `{"alignment": <D3 result>, "final": <F3 result>}`।

Hard constraint: এই ফাংশন **অডিও সেভ করে না** — সেটা এখনো
`voiceover_upload.save_uploaded_voiceover()`-এর কাজ (গ্রুপ D-তে wire হবে),
এই ফাংশন শুধু ধরে নেয় ফাইলটা ইতিমধ্যে ডিস্কে আছে। app.py স্পর্শ করবে না।

টেস্ট: existing D3/E2/F3 টেস্টের মকিং প্যাটার্নে — একটা fake
voiceover_hi.wav আগে থেকে সেভ করে রেখে `run_user_upload_chain(job_id)`
সরাসরি কল করো, assert করো `outputs/<job_id>/final_video.mp4` তৈরি হয়েছে।

শেষে (Definition of Done):
1. পুরো test suite পাশ করছে কনফার্ম করো।
2. `python3 -m py_compile pipeline/full_auto_chain.py`।
3. docs/HANDOFF_NEXT.md: "FA-B2 সম্পূর্ণ। পরের কাজ: FA-B3 (দুটো wrapper-এর
   error-handling + failure-case টেস্ট পলিশ, এখনো wiring না)।"
4. docs/CHANGELOG.md-এ এন্ট্রি যোগ করো।
5. Commit: "chunk FA-B2: run_user_upload_chain() — D3 to F3 in one function"
   — push করো।
6. Tag: git tag chunk-FA-B2-done && git push origin chunk-FA-B2-done

--- CHUNK FA-B3 ---

আমি manhwa-video-dubber-এর Full-Auto Pipeline আপডেটে কাজ করছি, "চাংক
FA-B3" (FA-B2 শেষ, গ্রুপ B-র শেষ চাংক)। এই চাংকের কাজ শুধু robustness +
টেস্ট — নতুন ফিচার না।

Repo: https://github.com/shafin262619-jpg/manhwa-video-dubber-v6.git
(branch: main)

প্রথমে করো:
1. Repo clone/pull করো (tag chunk-FA-B2-done থেকে verify করো)।
2. docs/HANDOFF_NEXT.md পড়ো।
3. pipeline/full_auto_chain.py (দুটো ফাংশনই) পড়ো।
4. app.py-র `_run_voiceover_auto()`/`_run_final_render()`-এ exception
   handling দেখো (কোন exception type ধরা হয়: FileNotFoundError,
   ValueError, RuntimeError, auto_cut.DraftValidationError) — এই একই
   pattern এখানে দরকার।

তোমার স্কোপ:

1. `run_auto_tts_chain()` আর `run_user_upload_chain()` — দুটোতেই নিশ্চিত
   করো ভেতরের প্রতিটা ধাপ থেকে আসা এই exception type-গুলো (FileNotFoundError,
   ValueError, RuntimeError, auto_cut.DraftValidationError) **uncaught না
   হয়ে caller পর্যন্ত propagate হয়** (গ্রুপ C/D পরে এগুলো ধরে
   job_status-এ error লিখবে) — কোনো silent swallow না, কোনো bare
   `except: pass` না।
2. মাঝ-চেইনে যেকোনো ধাপ ব্যর্থ হলে (যেমন TTS পুরোপুরি fail, বা draft
   validation fail, বা final render fail) — পরের ধাপ চালানো হবে না,
   exception সাথে সাথে propagate হবে (partial/silent state এড়াতে)।
3. pipeline/tests/test_full_auto_chain.py-কে **সম্পূর্ণ চূড়ান্ত টেস্ট
   স্যুট** বানাও:
   - happy path (FA-B1/B2-এর টেস্ট, ইতিমধ্যে আছে)
   - TTS সম্পূর্ণ ব্যর্থ (auto_tts chain) → exception propagate হয়,
     final_video.mp4 তৈরি হয় না
   - draft validation ব্যর্থ (auto_cut.DraftValidationError) → একইভাবে
   - final render (F3) ব্যর্থ (যেমন ffprobe duration mismatch) → একইভাবে
   - user_upload chain-এ align (D3) ব্যর্থ → exception propagate হয়

এই ধাপ শেষে pipeline/full_auto_chain.py-তে কোনো TODO/placeholder থাকবে
না — গ্রুপ B সম্পূর্ণ ও fully tested ধরা হবে।

শেষে (Definition of Done):
1. পুরো test suite পাশ করছে কনফার্ম করো, কোনো regression নেই।
2. docs/HANDOFF_NEXT.md: "গ্রুপ B (orchestration wrapper, F3 সহ, দুটো
   পাথ) সম্পূর্ণ ও fully tested, standalone (app.py এখনো অপরিবর্তিত)।
   পরের কাজ: গ্রুপ C (auto_tts পাথ HTTP-ওয়্যারিং, FA-C1 থেকে)।"
3. docs/CHANGELOG.md-এ এন্ট্রি যোগ করো।
4. Commit: "chunk FA-B3: error-handling polish + complete failure-case
   test suite for full_auto_chain" — push করো।
5. Tag: git tag chunk-FA-B3-done && git push origin chunk-FA-B3-done

--- CHUNK FA-C1 ---

আমি manhwa-video-dubber-এর Full-Auto Pipeline আপডেটে কাজ করছি, "চাংক
FA-C1" (FA-B3 শেষ, গ্রুপ B সম্পূর্ণ)। এটা সবচেয়ে ঝুঁকিপূর্ণ চাংক — existing
background-thread wiring বদলাচ্ছে, তাই সাবধানে করো।

Repo: https://github.com/shafin262619-jpg/manhwa-video-dubber-v6.git
(branch: main)

প্রথমে করো:
1. Repo clone/pull করো (tag chunk-FA-B3-done থেকে verify করো)।
2. docs/HANDOFF_NEXT.md পড়ো।
3. app.py-র `_run_upload_pipeline()` পুরোটা পড়ো — এটাই এই চাংকের মূল
   পরিবর্তনের জায়গা।
4. app.py-র `job_status_store.write_status()` কীভাবে stage/state/extra
   লেখে সেই প্যাটার্ন বোঝো (`_run_voiceover_auto()`-তে উদাহরণ আছে)।
5. pipeline/full_auto_chain.py-র `run_auto_tts_chain()` (FA-B1) পড়ো।

তোমার স্কোপ:

`_run_upload_pipeline(job_id)`-এর একদম শেষে (existing B1→B2→C1 চেইন +
`job_status_store.write_status(job_id, "upload_pipeline", "done", extra=extra)`
লেখার ঠিক পরে), এই লজিক যোগ করো — **একই থ্রেডে, নতুন থ্রেড স্পন না করে**:

    voice_source = voiceover_unify.get_voice_source(job_id)
    if voice_source == "auto_tts":
        job_status_store.write_status(job_id, "auto_full_render", "running")
        try:
            budget = gemini_rotation.CallBudget(config.MAX_API_CALLS_PER_JOB)
            result = full_auto_chain.run_auto_tts_chain(job_id, call_budget=budget)
            job_status_store.write_status(
                job_id, "auto_full_render", "done", extra={"result": result}
            )
        except (FileNotFoundError, ValueError, RuntimeError,
                auto_cut.DraftValidationError) as exc:
            job_status_store.write_status(
                job_id, "auto_full_render", "error",
                extra={"detail": _friendly_error(exc)},
            )

voice_source == "user_upload" (বা কোনো কারণে সেট না থাকলে) হলে **কিছুই
নতুন করবে না** — existing আচরণ (upload_pipeline done-এই থামা) অক্ষত থাকবে,
পরের গ্রুপ (D) সেটা হ্যান্ডল করবে।

Hard constraint:
- নতুন থ্রেড স্পন করবে না — `_run_upload_pipeline` নিজেই ইতিমধ্যে একটা
  background daemon thread-এ চলছে (existing `/upload` route থেকে), এই
  নতুন কোড সেই একই থ্রেডে সরাসরি চলবে (`_run_voiceover_auto()`-এর মতোই
  ভেতরে try/except বাধ্যতামূলক, thread যেন কখনো uncaught exception-এ মরে
  না যায়)।
- এই চাংকে `upload_status_page()`/polling page স্পর্শ করবে না — এটা
  পরের চাংক (FA-C2)। এখন শুধু status ঠিকমতো লেখা হচ্ছে কিনা নিশ্চিত করো
  (`GET /api/jobs/{job_id}/status` দিয়ে ভেরিফাই করা যাবে)।
- `voice_source == "user_upload"` পাথ এই চাংকে **একদম স্পর্শ করবে না**।

টেস্ট: HTTP TestClient দিয়ে — POST /upload voice_source="auto_tts" সহ
(mocked Gemini/ffmpeg) পাঠিয়ে, `GET /api/jobs/{id}/status` পোল করে
assert করো stage শেষে "auto_full_render"/"done" হয় আর
`outputs/<job_id>/final_video.mp4` তৈরি হয়েছে — **কোনো অন্য endpoint কল
না করেই** (এটাই zero-click প্রমাণ)। voice_source="user_upload" দিলে
stage "upload_pipeline"/"done"-এই থামে (নতুন কিছু ঘটে না) — সেটাও যাচাই
করো।

শেষে (Definition of Done):
1. পুরো test suite পাশ করছে কনফার্ম করো, কোনো regression নেই।
2. `python3 -m py_compile app.py`।
3. docs/HANDOFF_NEXT.md: "FA-C1 সম্পূর্ণ — auto_tts voice_source হলে
   upload-এর পরেই zero-click ফাইনাল ভিডিও পর্যন্ত ব্যাকএন্ড চেইন চলে।
   Polling page এখনো এটা reflect করে না (গ্রুপ B/C ফলাফল দেখায় না) —
   সেটা FA-C2।"
4. docs/CHANGELOG.md-এ এন্ট্রি যোগ করো।
5. Commit: "chunk FA-C1: auto_full_render stage — same-thread chain to F3
   for auto_tts" — push করো।
6. Tag: git tag chunk-FA-C1-done && git push origin chunk-FA-C1-done

--- CHUNK FA-C2 ---

আমি manhwa-video-dubber-এর Full-Auto Pipeline আপডেটে কাজ করছি, "চাংক
FA-C2" (FA-C1 শেষ)।

Repo: https://github.com/shafin262619-jpg/manhwa-video-dubber-v6.git
(branch: main)

প্রথমে করো:
1. Repo clone/pull করো (tag chunk-FA-C1-done থেকে verify করো)।
2. docs/HANDOFF_NEXT.md পড়ো।
3. app.py-র `upload_status_page()` আর `_render_final_result()` পড়ো —
   দ্বিতীয়টার markup এখন এখানে reuse হবে।

তোমার স্কোপ:

`upload_status_page(job_id)` আপডেট করো:
1. প্রথমে `voiceover_unify.get_voice_source(job_id)` চেক করো।
2. `voice_source == "auto_tts"` হলে — টার্গেট স্টেট এখন
   stage=="auto_full_render" and state=="done" (আগের মতো শুধু
   "upload_pipeline"/"done" না)। এখনো সেই স্টেটে না পৌঁছালে existing
   `_polling_page(...)` দেখাও (একই ফাংশন, শুধু স্টেজ-চেক বদলাচ্ছে)।
   পৌঁছে গেলে — "Continue: choose voiceover source" লিংক-ওয়ালা পুরনো
   result page-এর বদলে, `_render_final_result(job_id)`-এর মতোই ফাইনাল
   ভিডিও প্লেয়ার + ডাউনলোড লিংক সরাসরি দেখাও (কোড ডুপ্লিকেট না করে
   `_render_final_result()` reuse করো, `render_final`-এর result payload
   `auto_full_render` stage-এর `extra.result.final`-এ পাওয়া যাবে — দরকারে
   ছোট adapter লিখো)।
3. `voice_source == "user_upload"` (বা None/legacy) হলে — **এই চাংকে
   কিছু বদলাবে না**, existing "upload_pipeline done" → "Continue: choose
   voiceover source" আচরণ অক্ষত থাকবে (গ্রুপ D পরে এটা বদলাবে)।

Hard constraint: `/voiceover/{job_id}/choose`, `/final/{job_id}` — এই
পুরনো রুটগুলো **ডিলিট করবে না**, URL দিয়ে সরাসরি গেলে এখনো কাজ করা উচিত
(backward-compat/ম্যানুয়াল override হিসেবে, গ্রুপ E-তে ভেরিফাই হবে)।

টেস্ট: HTTP TestClient — POST /upload voice_source="auto_tts" (mocked)
→ পোল করো → assert করো একটা নির্দিষ্ট সংখ্যক পোলের পরে
`GET /upload/{job_id}`-এর HTML response-এ ফাইনাল ভিডিওর `<video>`/
ডাউনলোড লিংক আছে, "choose voiceover source" লিংক **নেই**। voice_source=
"user_upload" কেসে পুরনো "choose voiceover source" লিংক এখনো আছে যাচাই
করো (কিছু না ভাঙার প্রমাণ)।

শেষে (Definition of Done):
1. পুরো test suite পাশ করছে কনফার্ম করো।
2. `python3 -m py_compile app.py`।
3. docs/HANDOFF_NEXT.md: "গ্রুপ C (Auto-TTS পাথ) সম্পূর্ণ — auto_tts
   বেছে নিয়ে আপলোড করলে ইউজার আর কোনো ক্লিক ছাড়াই সরাসরি ফাইনাল ভিডিও
   দেখেন। পরের কাজ: গ্রুপ D (নিজের-অডিও পাথ, FA-D1 থেকে)।"
4. docs/CHANGELOG.md-এ এন্ট্রি যোগ করো।
5. Commit: "chunk FA-C2: upload status page shows final video directly
   for auto_tts" — push করো।
6. Tag: git tag chunk-FA-C2-done && git push origin chunk-FA-C2-done

--- CHUNK FA-D1 ---

আমি manhwa-video-dubber-এর Full-Auto Pipeline আপডেটে কাজ করছি, "চাংক
FA-D1" (FA-C2 শেষ, গ্রুপ C সম্পূর্ণ)।

Repo: https://github.com/shafin262619-jpg/manhwa-video-dubber-v6.git
(branch: main)

প্রথমে করো:
1. Repo clone/pull করো (tag chunk-FA-C2-done থেকে verify করো)।
2. docs/HANDOFF_NEXT.md পড়ো।
3. app.py-র `upload_status_page()` (FA-C2-এ আপডেট হয়েছে) আর
   `voiceover_choose()`-এর mode=="user_upload" ব্র্যাঞ্চের HTML (অডিও
   আপলোড ফর্ম, SRT/TXT রেফারেন্স লিংকসহ) পড়ো — সেই ফর্মটাই এখন এখানে
   সরাসরি বসবে।

লক্ষ্য: `voice_source=="user_upload"`-এর জন্য PRD-অনুযায়ী **একমাত্র থামা
পয়েন্ট** হওয়া উচিত অডিও আপলোড — কিন্তু এখন সেখানে পৌঁছানোর আগেই একটা
"Continue: choose voiceover source" লিংকে ক্লিক করতে হয় (যদিও voice_source
ইতিমধ্যেই upload-এর সময় সেভ হয়ে গেছে, FA-A1 থেকে)। এই চাংক সেই অতিরিক্ত
ক্লিকটা সরিয়ে দেবে।

তোমার স্কোপ:

`upload_status_page(job_id)`-এ (FA-C2-এর voice_source=="user_upload"
ব্র্যাঞ্চে) — "Continue: choose voiceover source →" লিংকের বদলে সরাসরি
অডিও-আপলোড ফর্ম বসাও (existing `voiceover_choose()`-এর
mode=="user_upload" HTML থেকে reuse/copy করো: SRT/TXT ডাউনলোড লিংক +
`<form method="post" action="/voiceover/{job_id}/upload" ...>`)।

Hard constraint: `/voiceover/{job_id}/choose` রুট **ডিলিট করবে না** —
এখনো URL দিয়ে সরাসরি গেলে কাজ করবে (ম্যানুয়াল override/backward-compat)।
এই চাংকে `/voiceover/{job_id}/upload` POST handler স্পর্শ করবে না —
সেটা পরের চাংক FA-D2।

টেস্ট: HTTP TestClient — POST /upload voice_source="user_upload" দিয়ে,
B1/B2/C1 শেষ হওয়া পর্যন্ত পোল করো, তারপর `GET /upload/{job_id}`-এর HTML-এ
assert করো অডিও-আপলোড `<form>` সরাসরি আছে, "choose voiceover source"
লিংক নেই।

শেষে (Definition of Done):
1. পুরো test suite পাশ করছে কনফার্ম করো।
2. `python3 -m py_compile app.py`।
3. docs/HANDOFF_NEXT.md: "FA-D1 সম্পূর্ণ — user_upload পাথে upload শেষে
   সরাসরি অডিও-ফর্ম দেখানো হয়, আলাদা 'choose' ক্লিক লাগে না। পরের কাজ:
   FA-D2 (অডিও POST-এর পর automatic zero-click ফাইনাল ভিডিও পর্যন্ত)।"
4. docs/CHANGELOG.md-এ এন্ট্রি যোগ করো।
5. Commit: "chunk FA-D1: post-upload page shows audio-upload form
   directly for user_upload" — push করো।
6. Tag: git tag chunk-FA-D1-done && git push origin chunk-FA-D1-done

--- CHUNK FA-D2 ---

আমি manhwa-video-dubber-এর Full-Auto Pipeline আপডেটে কাজ করছি, "চাংক
FA-D2" (FA-D1 শেষ)। এটা PRD-এর মূল requirement সম্পূর্ণ করার শেষ ওয়্যারিং
চাংক — এর পরে "নিজের অডিও" পাথে সত্যিই ঠিক একটাই থামা থাকবে।

Repo: https://github.com/shafin262619-jpg/manhwa-video-dubber-v6.git
(branch: main)

প্রথমে করো:
1. Repo clone/pull করো (tag chunk-FA-D1-done থেকে verify করো)।
2. docs/HANDOFF_NEXT.md পড়ো।
3. app.py-র `upload_voiceover()` (বর্তমান POST /voiceover/{job_id}/upload
   handler) আর `_run_voiceover_auto()`/`_polling_page()` প্যাটার্ন পড়ো।
4. pipeline/full_auto_chain.py-র `run_user_upload_chain()` (FA-B2) পড়ো।

তোমার স্কোপ:

`upload_voiceover(job_id, audio)` বদলাও:
- `voiceover_upload.save_uploaded_voiceover(...)` সফল হওয়ার পর, বর্তমান
  "Voiceover saved — Align subtitles to this audio (D3)" লিংক-ওয়ালা পেজ
  রিটার্ন করার বদলে —
- একটা নতুন background daemon thread শুরু করো (existing `_start_stage`/
  `threading.Thread(daemon=True)` প্যাটার্নে) যেটা চালাবে:

      def _run_user_audio_pipeline(job_id):
          try:
              result = full_auto_chain.run_user_upload_chain(job_id)
              job_status_store.write_status(
                  job_id, "user_audio_pipeline", "done",
                  extra={"result": result},
              )
          except (FileNotFoundError, ValueError, RuntimeError,
                  auto_cut.DraftValidationError) as exc:
              job_status_store.write_status(
                  job_id, "user_audio_pipeline", "error",
                  extra={"detail": _friendly_error(exc)},
              )

  থ্রেড শুরুর আগে `job_status_store.write_status(job_id,
  "user_audio_pipeline", "running")` লেখো।
- `upload_voiceover()` তখন `_polling_page(job_id, "Processing your
  audio", f"/upload/{job_id}", "user_audio_pipeline")` রিটার্ন করবে
  (existing polling page reuse, শুধু নতুন stage নাম)।

`upload_status_page(job_id)`-এ (FA-C2/D1-এ যেটা আছে তার ওপর) আরেকটা চেক
যোগ করো: stage=="user_audio_pipeline" and state=="done" হলে —
`_render_final_result()`-এর মতোই ফাইনাল ভিডিও সরাসরি দেখাও (ঠিক যেভাবে
FA-C2-এ auto_full_render-এর জন্য করেছিলে, একই adapter reuse/অনুসরণ
করো)।

Hard constraint:
- `/voiceover/{job_id}/align_uploaded` GET রুট **ডিলিট করবে না** — এখনো
  ম্যানুয়ালি re-align করার জন্য কাজ করবে (backward-compat)।
- এই থ্রেড কখনো uncaught exception-এ মরবে না (try/except বাধ্যতামূলক,
  existing daemon-thread কনভেনশন অনুযায়ী)।
- অডিও ফাইল ফরম্যাট/সাইজ ভ্যালিডেশন (existing
  `UnsupportedAudioError`) অপরিবর্তিত থাকবে।

টেস্ট: HTTP TestClient — সম্পূর্ণ end-to-end: POST /upload
voice_source="user_upload" → পোল (B1/B2/C1 শেষ) → POST
/voiceover/{job_id}/upload একটা fake wav ফাইল সহ → পোল
(user_audio_pipeline done পর্যন্ত) → assert করো ফাইনাল ভিডিও
ডাউনলোডযোগ্য (`GET /download/{job_id}`) — **এই তিনটা POST/GET
(upload, voiceover-upload, status-poll) ছাড়া আর কোনো endpoint কল না
করেই।**

শেষে (Definition of Done):
1. পুরো test suite পাশ করছে কনফার্ম করো।
2. `python3 -m py_compile app.py`।
3. docs/HANDOFF_NEXT.md: "গ্রুপ D সম্পূর্ণ — PRD-এর মূল requirement এখন
   পুরোপুরি বাস্তবায়িত: auto_tts পাথে zero-click, user_upload পাথে
   ঠিক একটাই থামা (অডিও আপলোড)। পরের কাজ: গ্রুপ E (ব্যাকওয়ার্ড-কম্প্যাট
   অডিট + নতুন E2E রিগ্রেশন টেস্ট)।"
4. docs/CHANGELOG.md-এ এন্ট্রি যোগ করো।
5. Commit: "chunk FA-D2: audio upload auto-continues to final video
   (single pause point complete)" — push করো।
6. Tag: git tag chunk-FA-D2-done && git push origin chunk-FA-D2-done

--- CHUNK FA-E1 ---

আমি manhwa-video-dubber-এর Full-Auto Pipeline আপডেটে কাজ করছি, "চাংক
FA-E1" (FA-D2 শেষ, গ্রুপ D সম্পূর্ণ)। এই চাংকের কাজ শুধু ভেরিফিকেশন +
বাগফিক্স — নতুন ফিচার না।

Repo: https://github.com/shafin262619-jpg/manhwa-video-dubber-v6.git
(branch: main)

প্রথমে করো:
1. Repo clone/pull করো (tag chunk-FA-D2-done থেকে verify করো)।
2. docs/HANDOFF_NEXT.md পড়ো।
3. docs/GITHUB_AGENT_HANDOFF_PLAN_FULL_AUTO.md-এর গ্রুপ A-D পুরোটা আবার
   পড়ো (কী কী পুরনো রুট/ফাংশন "ডিলিট করবে না" বলা হয়েছিল, সব লিস্ট করো)।

তোমার স্কোপ — এই checklist ম্যানুয়ালি (টেস্টসহ) যাচাই করো:

1. `voice_source` param ছাড়া POST /upload করলে — ডিফল্ট "auto_tts"
   (FA-A1) সেট হয় ঠিকই, কিন্তু **পুরনো ব্যবহারকারী যদি সরাসরি পুরনো
   ম্যানুয়াল রুটগুলো ব্যবহার করে** (`/voiceover/{id}/choose` দিয়ে mode
   আবার বদলে দেয়) — সেই override সম্মান করা হয় কিনা যাচাই করো (অর্থাৎ
   FA-C1-এর auto_full_render logic যদি ইতিমধ্যে চলে গিয়ে থাকে, ম্যানুয়াল
   override যেন crash না করে বা duplicate render না করে — resumable/
   idempotent থাকতে হবে, existing `_process_auto_tts`-এর idempotent
   প্যাটার্নে)।
2. `/voiceover/{job_id}/choose`, `/voiceover/{job_id}/align_uploaded`,
   `/final/{job_id}`, `/review/{job_id}`, `/review/{job_id}/edit` —
   সবগুলো এখনো URL দিয়ে সরাসরি গেলে কাজ করে যাচাই করো (existing G1
   orchestration test-এর পুরনো flow দিয়ে)।
3. **পুরো existing test suite (২৭০+, U0-U5) কোনো পরিবর্তন ছাড়াই এখনো
   পাশ করে** কনফার্ম করো — কোনো regression পেলে বাগফিক্স করো (নতুন ফিচার
   যোগ করবে না)।
4. `pipeline/dry_run_check.py` (U5) নতুন flow-এর সাথেও কাজ করে যাচাই
   করো (job artifacts-এর নাম/লোকেশন এই চাংকগুলোতে বদলায়নি বলে এটা
   স্বাভাবিকভাবেই কাজ করা উচিত — শুধু কনফার্ম করো)।

শেষে (Definition of Done):
1. উপরের checklist-এর প্রতিটা পয়েন্ট পাশ/ফেইল স্পষ্ট করে ডকুমেন্ট করো
   (docs/HANDOFF_NEXT.md-এ)। কোনো ফেইল পেলে ফিক্স করে আবার যাচাই করো।
2. পুরো test suite পাশ করছে কনফার্ম করো।
3. docs/HANDOFF_NEXT.md: "FA-E1 ব্যাকওয়ার্ড-কম্প্যাট অডিট সম্পূর্ণ, সব
   পুরনো ম্যানুয়াল রুট অক্ষত। পরের কাজ: FA-E2 (নতুন zero-click/single-pause
   পাথ দুটোর জন্য permanent E2E regression test)।"
4. docs/CHANGELOG.md-এ এন্ট্রি যোগ করো।
5. Commit: "chunk FA-E1: backward-compat audit + fixes" — push করো।
6. Tag: git tag chunk-FA-E1-done && git push origin chunk-FA-E1-done

--- CHUNK FA-E2 ---

আমি manhwa-video-dubber-এর Full-Auto Pipeline আপডেটে কাজ করছি, "চাংক
FA-E2" (FA-E1 শেষ)।

Repo: https://github.com/shafin262619-jpg/manhwa-video-dubber-v6.git
(branch: main)

প্রথমে করো:
1. Repo clone/pull করো (tag chunk-FA-E1-done থেকে verify করো)।
2. docs/HANDOFF_NEXT.md পড়ো।
3. pipeline/tests/test_app_orchestration.py (existing G1 regression,
   পুরনো ম্যানুয়াল flow টেস্ট করে) পুরোটা পড়ো — এটা **অপরিবর্তিত থাকবে**,
   এই চাংক শুধু পাশে নতুন টেস্ট যোগ করবে।

তোমার স্কোপ:

নতুন ফাইল `pipeline/tests/test_full_auto_orchestration.py` বানাও, G1-এর
মতোই HTTP-only (`TestClient`, mocked Gemini/ffmpeg, D2 real-ffmpeg-silence
প্যাটার্ন অনুসরণ করো), তাতে দুটো **permanent** end-to-end টেস্ট:

1. **`test_auto_tts_zero_click_end_to_end`** — POST /upload
   (voice_source="auto_tts") → শুধু `GET /api/jobs/{id}/status` পোল করে
   (অন্য কোনো endpoint কল না করে) → assert final video downloadable
   (`GET /download/{job_id}`)। মাঝে assert করো ঠিক কোন কোন stage-through
   গেছে (upload_pipeline → auto_full_render)।
2. **`test_user_upload_single_pause_end_to_end`** — POST /upload
   (voice_source="user_upload") → পোল (assert থামে "upload_pipeline
   done"-এ, auto-continue করে না) → POST
   /voiceover/{job_id}/upload (fake wav) → পোল (user_audio_pipeline
   done) → assert final video downloadable। মাঝে assert করো
   `/voiceover/{job_id}/choose`, `/voiceover/{job_id}/align_uploaded`,
   `/final/{job_id}` — এই তিনটার **কোনোটাই কল হয়নি** (single-pause
   claim-এর প্রমাণ)।

Hard constraint: পুরনো `test_app_orchestration.py` **এক লাইনও বদলাবে
না** — সেটা এখনো আলাদাভাবে পাশ করবে (ব্যাকওয়ার্ড-কম্প্যাট প্রমাণ হিসেবে
পাশাপাশি রাখা হচ্ছে, রিপ্লেস না)।

শেষে (Definition of Done):
1. পুরো test suite (G0-এর সব পুরনো + নতুন দুটো E2E সহ) পাশ করছে কনফার্ম
   করো।
2. docs/HANDOFF_NEXT.md: "FA-E2 সম্পূর্ণ — দুটো permanent E2E রিগ্রেশন
   টেস্ট (zero-click auto path, single-pause upload path) যোগ হয়েছে,
   পুরনো G1 টেস্টও অক্ষত। পরের কাজ: গ্রুপ F (ফুল রিগ্রেশন + ফাইনাল
   wrap-up)।"
3. docs/CHANGELOG.md-এ এন্ট্রি যোগ করো।
4. Commit: "chunk FA-E2: permanent E2E regression tests for both
   full-auto paths" — push করো।
5. Tag: git tag chunk-FA-E2-done && git push origin chunk-FA-E2-done

--- CHUNK FA-F1 ---

আমি manhwa-video-dubber-এর Full-Auto Pipeline আপডেটে কাজ করছি, "চাংক
FA-F1" (FA-E2 শেষ)। এই চাংকের একমাত্র কাজ verification + bugfix।

Repo: https://github.com/shafin262619-jpg/manhwa-video-dubber-v6.git
(branch: main)

প্রথমে করো:
1. Repo clone/pull করো (tag chunk-FA-E2-done থেকে verify করো)।
2. docs/HANDOFF_NEXT.md পড়ো।

তোমার স্কোপ:

1. **পুরো** test suite রান করো — `python3 -m unittest discover -s
   pipeline/tests -v` (U0-U5 + FA-A1 থেকে FA-E2 পর্যন্ত সবকিছু মিলিয়ে)।
   যেকোনো regression পেলে ঠিক করো — নতুন ফিচার যোগ করবে না, শুধু বাগফিক্স।
2. `python3 -m py_compile` দিয়ে touched হওয়া সব ফাইল যাচাই করো: app.py,
   pipeline/full_auto_chain.py, pipeline/voiceover_unify.py (যদি
   বদলে থাকে)।
3. `python3 -m pipeline.dry_run_check --job-id <একটা টেস্ট job-id>`
   (U5 tool) দিয়ে একটা fixture job-এ চালিয়ে নিশ্চিত করো নতুন flow-এর
   আউটপুট ফাইলগুলোর সাথেও এটা compatible।
4. চূড়ান্ত টেস্ট কাউন্ট আর pass/fail status স্পষ্ট করে ডকুমেন্ট করো।

শেষে (Definition of Done):
1. পুরো test suite ১০০% পাশ করছে (অথবা কোনটা পাশ করছে না আর কেন, যদি
   সমাধান সম্ভব না হয়, স্পষ্ট লেখো)।
2. docs/HANDOFF_NEXT.md: চূড়ান্ত টেস্ট-স্ট্যাটাস, পরের কাজ FA-F2 (final
   wrap-up)।
3. docs/CHANGELOG.md-এ এন্ট্রি যোগ করো।
4. Commit: "chunk FA-F1: full regression pass + fixes" — push করো।
5. Tag: git tag chunk-FA-F1-done && git push origin chunk-FA-F1-done

--- CHUNK FA-F2 ---

আমি manhwa-video-dubber-এর Full-Auto Pipeline আপডেটের একদম শেষ চাংক
"FA-F2"-এ কাজ করছি (FA-F1 শেষ, পুরো রিগ্রেশন পাশ করেছে)।

Repo: https://github.com/shafin262619-jpg/manhwa-video-dubber-v6.git
(branch: main)

প্রথমে করো:
1. Repo clone/pull করো (tag chunk-FA-F1-done থেকে verify করো)।
2. docs/HANDOFF_NEXT.md পড়ো।
3. docs/FINAL_SUMMARY.md (U-সিরিজের চূড়ান্ত সামারি) পড়ো — এই চাংক তাতে
   একটা নতুন সেকশন যোগ করবে, রিপ্লেস না।

তোমার স্কোপ:

1. docs/FINAL_SUMMARY.md-এ একটা নতুন সেকশন যোগ করো "## Full-Auto
   Pipeline (FA1-F2)" — তাতে থাকবে:
   - সংক্ষিপ্ত সারাংশ: এখন upload করলে auto_tts choice-এ zero-click
     ফাইনাল ভিডিও পর্যন্ত চলে, user_upload choice-এ শুধু অডিও-আপলোডেই
     থামে, তারপর আবার zero-click ফাইনাল ভিডিও পর্যন্ত।
   - কোন ফাইলে কী যোগ হয়েছে (pipeline/full_auto_chain.py নতুন,
     app.py-তে নতুন stage নাম `auto_full_render`/`user_audio_pipeline`)।
   - একটা স্পষ্ট নোট: **"এই একটা ধাপ কোনো sandboxed AI agent করতে পারবে
     না — ব্যবহারকারীকে নিজে করতে হবে"** —
     (ক) real Gemini key + একটা real ভিডিও দিয়ে auto_tts পাথে সত্যিই
     কোনো ক্লিক ছাড়াই ফাইনাল ভিডিও আসে কিনা browser-এ নিজের চোখে
         যাচাই করা;
     (খ) user_upload পাথে সত্যিই শুধু অডিও-আপলোডেই থামে, এরপর আর কোনো
         ক্লিক লাগে না কিনা যাচাই করা;
     (গ) ফাইনাল ভিডিওর কোয়ালিটি U-সিরিজের আগের আউটপুটের মতোই আছে
         (এই আপডেট UX/wiring বদলেছে, pipeline-এর আউটপুট কোয়ালিটি
         বদলায়নি) — স্পট-চেক করে কনফার্ম করা।
2. docs/HANDOFF_NEXT.md আপডেট করো: "সব FA চাংক (A1-F2) সম্পূর্ণ। PRD-এর
   মূল requirement (upload → zero-click ফাইনাল ভিডিও, নিজের অডিও দিতে
   চাইলে শুধু সেখানেই থামা) বাস্তবায়িত ও রিগ্রেশন-টেস্টেড। বাকি শুধু
   ব্যবহারকারীর নিজের real-media QA রান (উপরের ৩ পয়েন্ট)।"
3. docs/CHANGELOG.md-এ চূড়ান্ত এন্ট্রি যোগ করো।

শেষে:
1. Commit: "chunk FA-F2: final wrap-up + FINAL_SUMMARY.md full-auto
   section" — push করো।
2. Tag: git tag manhwa-video-dubber-v6-full-auto-final && git push origin
   manhwa-video-dubber-v6-full-auto-final

যদি context ফুরিয়ে যায় আর কাজ অসম্পূর্ণ থাকে, docs/HANDOFF_NEXT.md-এ
স্পষ্ট করে লিখো কোন অংশ সম্পূর্ণ আর কোনটা না — প্রয়োজনে এই চাংককেও নিজে
আরও ভেঙে (FA-F2-১, FA-F2-২...) পরের সেশনে চালিয়ে যাও, একই প্রোটোকল
অনুসরণ করে।
