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

## স্ট্যাটাস: FA-B2 সম্পূর্ণ

**FA-B2 সম্পূর্ণ।** `chunk-FA-B2-done` ট্যাগ বসানো হয়েছে।

এই চাংকে যা হলো:
- `pipeline/full_auto_chain.py`-তে `run_user_upload_chain(job_id)` ফাংশন —
  D3 (`voiceover_upload.align_uploaded_voiceover`) → D4 → E1 → E2 → F3 —
  রিটার্ন `{"alignment": <D3 result>, "final": <F3 result>}`।
- Hard constraint: ফাংশন **অডিও সেভ করে না** — সেটা
  `voiceover_upload.save_uploaded_voiceover()`-এর কাজ (গ্রুপ D-তে wire হবে),
  শুধু ধরে নেয় `voiceover_hi.wav` ডিস্কে আছে। app.py স্পর্শ করা হয়নি।
- টেস্ট: `test_full_auto_chain.py`-এ +১টা — আগে fake `voiceover_hi.wav` সেভ
  করে, Gemini align mock করে `run_user_upload_chain(job_id)` সরাসরি কল,
  `outputs/<job_id>/final_video.mp4` তৈরি হয় যাচাই করা। পুরো স্যুট এখন
  **২৭৫টা টেস্ট OK** (২৭৪ + ১)।

## পরের কাজ

**FA-B3 (দুটো wrapper-এর error-handling + failure-case টেস্ট পলিশ, এখনো
wiring না)।** বিস্তারিত `docs/FA_CHUNK_BATCH.md`-এর `--- CHUNK FA-B3 ---`
সেকশনে। এই চাংক শুধু robustness + টেস্ট — নতুন ফিচার না:
- `run_auto_tts_chain()` / `run_user_upload_chain()` — দুটোতেই
  FileNotFoundError / ValueError / RuntimeError / `auto_cut.DraftValidationError`
  caller পর্যন্ত propagate হয় (কোনো silent swallow / `except: pass` না)।
- মাঝ-চেইনে কোনো ধাপ ব্যর্থ হলে পরের ধাপ চালানো হবে না (exception সাথে
  সাথে propagate)।
- `test_full_auto_chain.py`-কে সম্পূর্ণ failure-case টেস্ট স্যুট বানানো
  (TTS fail, draft validation fail, final render fail, D3 align fail)। শেষে
  `pipeline/full_auto_chain.py`-তে কোনো TODO/placeholder থাকবে না।

পুরো data-flow/চলার নিয়ম: `docs/FINAL_SUMMARY.md`। চ্যাঞ্জলগ:
`docs/CHANGELOG.md`।
