# HANDOFF NEXT

গ্রুপ A-E (subtitle-QA-fix, A1-E4) + E5 (real-media QA-writeback fix) +
E6 (draft duration-validation fix) সম্পূর্ণ।

E6 (`manhwa-video-dubber-v6-duration-validation-fix`): Real-media QA-তে
audio-upload validation স্টেজের দুটো বাগ ঠিক করা হয়েছে (job
`705fea53-129e-4cfe-bc75-0e49ef356305` থেকে রিপোর্ট):

- **সমস্যা ১** — `expected_duration_sec` এখন draft-এর validation-এ আর
  voiceover audio-র duration না হয়ে আসল source video-র ffprobe-duration
  (job_meta.json থেকে) — একই single source of truth যা
  `subtitle_qa.json`-এর `total_duration_sec`-ও ব্যবহার করে। ফলে একই job-এর
  জন্য দুটো ভিন্ন number (৫০৪s বনাম ৩০৩s) আর দেখানো হবে না।
- **সমস্যা ২** — `user_upload` পাথে এখন loose, configurable tolerance
  (`USER_UPLOAD_DURATION_TOLERANCE_SEC = 3.0`, `USER_UPLOAD_DURATION_TOLERANCE_RATIO = 0.05`,
  এর মধ্যে বড়টা) প্রযোজ্য; auto-TTS পাথের কড়া frames-tolerance অপরিবর্তিত।
  বড় mismatch (২০+s) এখনো reject হয় (ভুল ফাইল আপলোডের সংকেত)।

বাকি কাজ:
- ব্যবহারকারীর নিজের real-media QA রান:
  - docs/FINAL_SUMMARY.md → "Subtitle QA Fixes (A1-E4)" → "The user must do this"
  - যে jobগুলো আগে zero-duration/duplicate-start SRT দিয়েছিল সেগুলো রি-রান করে
    confirm করো যে final `.srt`-এ আর zero/negative duration বা duplicate
    start-time নেই।
- `expected_duration_sec`-এর নতুন semantics মাথায় রেখে (draft ≈ source
  duration) যে job-গুলো `user_upload` দিয়ে আবার রান করবে সেগুলোর draft
  validation এখন হিউম্যান-রেকর্ডিংয়ের প্রাকৃতিক pacing variance গ্রহণ করে।
