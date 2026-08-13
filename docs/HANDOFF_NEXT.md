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

## স্ট্যাটাস: FA-A1 সম্পূর্ণ

**FA-A1 (upfront voice-source input) সম্পূর্ণ।** `chunk-FA-A1-done` ট্যাগ
বসানো হয়েছে।

এই চাংকে যা হলো:
- `home()` upload ফর্মে radio-group যোগ: `voice_source="auto_tts"` (ডিফল্ট
  checked) / `"user_upload"` — FormData-তে ফিল্ড স্বয়ংক্রিয়ভাবে যায়।
- `upload_video()` নতুন optional param `voice_source: str = Form("auto_tts")`
  নেয়; `ALLOWED_MODES`-এর বাইরে হলে 400; valid হলে upload সফল হওয়ার সাথে
  সাথেই (B1→B2→C1 background thread শুরুর আগে)
  `voiceover_unify.set_voice_source(job_id, voice_source)` দিয়ে
  `voice_source_choice.json`-এ সেভ হয়।
- Hard constraint মেনে চলা: `_run_upload_pipeline`, `voiceover_choose`,
  `/voiceover/{job_id}/choose` — সবের আচরণ অপরিবর্তিত (backward-compat,
  FA-E1-এ ভেরিফাই হবে)।
- টেস্ট: `pipeline/tests/test_full_auto_upload.py` (৩টা নতুন) — user_upload
  immediate persist, default auto_tts, invalid → 400। পুরো স্যুট এখন
  **২৭৩টা টেস্ট OK** (আগের ২৭০ + ৩)।

## পরের কাজ

**FA-B1 (auto_tts orchestration wrapper, পুরো D2→D4→E1→E2→F3 চেইন এক
ফাংশনে, এখনো routes-এ wire না)।** বিস্তারিত `docs/FA_CHUNK_BATCH.md`-এর
`--- CHUNK FA-B1 ---` সেকশনে। নতুন ফাইল
`pipeline/full_auto_chain.py`-তে `run_auto_tts_chain(job_id,
call_budget=None)` ফাংশন বানাতে হবে, যা voiceover_auto →
voiceover_unify → edit_guideline → auto_cut → render_final (F3 সহ) এক
সাথে চালায়; app.py স্পর্শ করা যাবে না; টেস্ট + commit + push +
`chunk-FA-B1-done` ট্যাগ।

পুরো data-flow/চলার নিয়ম: `docs/FINAL_SUMMARY.md`। চ্যাঞ্জলগ:
`docs/CHANGELOG.md`।
