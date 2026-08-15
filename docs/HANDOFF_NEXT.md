# HANDOFF NEXT

গ্রুপ A-E (subtitle-QA-fix, A1-E4) + E5 (real-media QA-writeback fix) সম্পূর্ণ।

E5 (`manhwa-video-dubber-v6-qa-repair-writeback-fix`): single zero-duration
entries এখন flagged + repaired হয়, তাই `subtitle_qa.json`-এর repair success
আর final `subtitles_hi.srt`-এর মধ্যে mismatch থাকবে না। Real job
`edb1b1ef-5041-491e-bb3f-c8aa3617794a`-এর reported symptom (repair "succeeded"
2 কিন্তু SRT-তে serial 89/154 zero-duration) ঠিক করা হয়েছে।

বাকি শুধু ব্যবহারকারীর নিজের real-media QA রান:
- docs/FINAL_SUMMARY.md → "Subtitle QA Fixes (A1-E4)" → "The user must do this"
- যে jobগুলো আগে zero-duration/duplicate-start SRT দিয়েছিল সেগুলো রি-রান করে
  confirm করো যে final `.srt`-এ আর zero/negative duration বা duplicate
  start-time নেই।
