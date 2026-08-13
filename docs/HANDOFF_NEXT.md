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

## স্ট্যাটাস: FA-D1 সম্পূর্ণ

**FA-D1 সম্পূর্ণ — user_upload পাথে upload শেষে সরাসরি অডিও-ফর্ম দেখানো
হয়, আলাদা "choose" ক্লিক লাগে না।** `chunk-FA-D1-done` ট্যাগ বসানো
হয়েছে।

এই চাংকে যা হলো:
- `upload_status_page()`-এর user_upload ব্রাঞ্চে "Continue: choose
  voiceover source" লিংকের বদলে সরাসরি অডিও-আপলোড ফর্ম বসানো হয়েছে
  (SRT/TXT রেফারেন্স লিংক + `<form action="/voiceover/{job_id}/upload">`),
  `voiceover_choose()`-এর mode=="user_upload" markup থেকে reuse।
- Hard constraint: `/voiceover/{job_id}/choose` রুট ডিলিট করা হয়নি
  (URL দিয়ে সরাসরি গেলে এখনো কাজ করে); `/voiceover/{job_id}/upload` POST
  handler স্পর্শ করা হয়নি (ওটা FA-D2)।
- টেস্ট: FA-C2-এর user_upload page টেস্ট FA-D1 আচরণে আপডেট — অডিও ফর্ম
  সরাসরি আছে (action + multipart), "choose voiceover source" লিংক নেই,
  `<video>` নেই। পুরো স্যুট এখন **২৮৩টা টেস্ট OK** (সংখ্যা অপরিবর্তিত —
  টেস্ট আপডেট, যোগ না)।

## পরের কাজ

**FA-D2 (অডিও POST-এর পর automatic zero-click ফাইনাল ভিডিও পর্যন্ত)।**
বিস্তারিত `docs/FA_CHUNK_BATCH.md`-এর `--- CHUNK FA-D2 ---` সেকশনে:
- `/voiceover/{job_id}/upload` POST handler-এ — অডিও সেভ হওয়ার পর (একই
  থ্রেডে) `full_auto_chain.run_user_upload_chain(job_id)` চালিয়ে
  `user_full_render` stage-এ running/done/error লিখতে হবে (error-এ
  friendly message), যাতে ইউজার upload-এর পর আর কোনো ক্লিক ছাড়া ফাইনাল
  ভিডিও পান; তারপর `/upload/{job_id}` পেজ সেই stage শেষে ফাইনাল ভিডিও
  দেখাবে (FA-C2-এর auto_tts প্যাটার্ন)।
- Hard constraint: app.py-তে user_upload-এর অডিও-আপলোড UI ব্রাঞ্চ ভাঙা
  যাবে না।

পুরো data-flow/চলার নিয়ম: `docs/FINAL_SUMMARY.md`। চ্যাঞ্জলগ:
`docs/CHANGELOG.md`।
