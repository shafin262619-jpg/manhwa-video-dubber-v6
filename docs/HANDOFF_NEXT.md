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

## স্ট্যাটাস: গ্রুপ C (Auto-TTS পাথ) সম্পূর্ণ

**গ্রুপ C সম্পূর্ণ — auto_tts বেছে নিয়ে আপলোড করলে ইউজার আর কোনো ক্লিক
ছাড়াই সরাসরি ফাইনাল ভিডিও দেখেন।** `chunk-FA-C2-done` ট্যাগ বসানো হয়েছে।

এই চাংকে যা হলো:
- `upload_status_page(job_id)` এখন `get_voice_source(job_id)` দেখে:
  - `auto_tts`: `auto_full_render`/`done` না হওয়া পর্যন্ত existing
    `_polling_page(...)` (শুধু target stage `auto_full_render`); শেষ হলে
    সরাসরি ফাইনাল ভিডিও প্লেয়ার + ডাউনলোড লিংক — `_render_final_result()`
    reuse (এখন optional `result` dict নেয়; chain-এর
    `auto_full_render.result.final` payload adapter হিসেবে পাঠানো হয়)।
  - `user_upload` / None (legacy): অপরিবর্তিত — পুরনো "Continue: choose
    voiceover source" আচরণ অক্ষত (গ্রুপ D পরে বদলাবে)।
- `_polling_page()`-এর error branch এখন polled stage অনুপস্থিত থাকলে current
  stage-এর detail দেখায় (যেমন auto_tts পোলিং পেজে early B1/B2/C1 fail-এ
  "Unknown error."-এর বদলে আসল error)।
- `/voiceover/{job_id}/choose` ও `/final/{job_id}` রুট ডিলিট করা হয়নি —
  সরাসরি URL-এ গেলে এখনো কাজ করে (manual override/backward-compat, গ্রুপ
  E-তে verify হবে)।
- টেস্ট: +২টা HTTP টেস্ট — auto_tts-এ `GET /upload/{job_id}` শেষে `<video>`
  + download লিংক আছে, "choose voiceover source" লিংক নেই; user_upload-এ
  পুরনো লিংক আছে + `<video>` নেই। পুরো স্যুট এখন **২৮৩টা টেস্ট OK**
  (২৮১ + ২)।

## পরের কাজ

**গ্রুপ D (নিজের-অডিও পাথ, FA-D1 থেকে)।** বিস্তারিত
`docs/FA_CHUNK_BATCH.md`-এর `--- CHUNK FA-D1 ---` সেকশনে:
- `upload_status_page()`-এর user_upload ব্রাঞ্চে "Continue: choose
  voiceover source" লিংকের বদলে সরাসরি অডিও-আপলোড ফর্ম বসানো
  (`voiceover_choose()`-এর mode=="user_upload" HTML থেকে reuse: SRT/TXT
  ডাউনলোড লিংক + `<form action="/voiceover/{job_id}/upload">`) — voice_source
  তো FA-A1-তেই সেভ হয়ে গেছে, আলাদা ক্লিক লাগবে না।
- Hard constraint: `/voiceover/{job_id}/choose` রুট ডিলিট করা যাবে না;
  FA-D1-এ `/voiceover/{job_id}/upload` POST handler স্পর্শ করা যাবে না (ওটা
  FA-D2)।

পুরো data-flow/চলার নিয়ম: `docs/FINAL_SUMMARY.md`। চ্যাঞ্জলগ:
`docs/CHANGELOG.md`।
