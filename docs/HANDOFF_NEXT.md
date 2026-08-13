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

## স্ট্যাটাস: FA-B1 সম্পূর্ণ

**FA-B1 (auto_tts orchestration wrapper, F3 সহ) সম্পূর্ণ, standalone, এখনো
app.py-তে wire হয়নি।** `chunk-FA-B1-done` ট্যাগ বসানো হয়েছে।

এই চাংকে যা হলো:
- নতুন ফাইল `pipeline/full_auto_chain.py`-এ `run_auto_tts_chain(job_id,
  call_budget=None)` ফাংশন — D2 (`voiceover_auto.generate_auto_voiceover`)
  → D4 (`voiceover_unify.unify_voiceover_timestamps`) → E1
  (`edit_guideline.build_edit_guideline`) → E2 (`auto_cut.build_draft_video`)
  → **F3 (`render_final.finalize_video`)** — এক ফাংশনে, রিটার্ন
  `{"voiceover": <D2 result>, "final": <F3 result>}`।
- Hard constraint: app.py স্পর্শ করা হয়নি — `_process_auto_tts` /
  `_continue_from_voiceover` অপরিবর্তিত (backward-compat, FA-E1-এ ভেরিফাই
  হবে)। নতুন ফাংশন এখনো কোনো route-এ কল হয়নি।
- টেস্ট: `pipeline/tests/test_full_auto_chain.py` — Gemini TTS + ffmpeg mock
  করে সরাসরি কল, `outputs/<job_id>/final_video.mp4` তৈরি হয় যাচাই করা।
  পুরো স্যুট এখন **২৭৪টা টেস্ট OK** (২৭৩ + ১)।

## পরের কাজ

**FA-B2 (user_upload-এর জন্য একই প্যাটার্নের wrapper)।** বিস্তারিত
`docs/FA_CHUNK_BATCH.md`-এর `--- CHUNK FA-B2 ---` সেকশনে।
`pipeline/full_auto_chain.py`-তে `run_user_upload_chain(job_id)` যোগ করতে
হবে — D3 (`voiceover_upload.align_uploaded_voiceover`) → D4 → E1 → E2 → F3,
রিটার্ন `{"alignment": <D3 result>, "final": <F3 result>}`; অডিও সেভ এই
ফাংশনের কাজ না (গ্রুপ D-তে wire হবে); app.py স্পর্শ করা যাবে না; টেস্ট +
commit + push + `chunk-FA-B2-done` ট্যাগ।

পুরো data-flow/চলার নিয়ম: `docs/FINAL_SUMMARY.md`। চ্যাঞ্জলগ:
`docs/CHANGELOG.md`।
