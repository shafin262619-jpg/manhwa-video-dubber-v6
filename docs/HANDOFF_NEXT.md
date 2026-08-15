# HANDOFF NEXT

গ্রুপ A-E (subtitle-QA-fix, A1-E4) + E5 (real-media QA-writeback fix) +
E6 (draft duration-validation fix) + E7 (cascade-crash fix) সম্পূর্ণ।

E7 (`manhwa-video-dubber-v6-cascade-crash-fix`): Real-media QA-তে পুরো-জব
crash করা cascade bug ঠিক করা হয়েছে (job
`97a9b90e-71f4-4d64-931d-b1b5cd194ce2` থেকে রিপোর্ট):

- **Fix B (root cause)** — `subtitle_builder._serialize`-এ ৩+ পরপর
  overlap-collision-run আগে একটার পর একটা `prev_end`-এ clamp হয়ে সবাই একই
  zero-length timestamp-এ (যেমন `[100.000..100.000]`) collapse হয়ে যেত;
  এখন `_redistribute_collision_cluster` পুরো cluster-কে non-zero,
  text-length-weighted duration-এ redistribute করে (পরের anchor-entry-এ
  জায়গা থাকলে proportional, না থাকলে per-entry fallback min
  `SUBTITLE_MIN_SERIAL_DURATION_SEC = 0.8s`)। নতুন
  `detect_collision_clusters()` `reason: "collision_cluster"`-সহ
  `subtitle_qa.json`-এ আলাদা diagnostic flag দেয় (whisper cross-check
  ফলাফলেও আসে)।
- **Fix A (crash guard)** — `auto_cut` এখন `RENDER_MIN_SEGMENT_DURATION_SEC`
  (0.05s)-এর চেয়ে ছোট source segment-কে minimal real window-এ কেটে
  target-এ stretch করে, ফলে ffmpeg-এর `-to value smaller than -ss` abort
  আর পুরো job fail করতে পারে না।
- **Fix C (readable error)** — `_extract_ffmpeg_error` ffmpeg stderr-এর
  version banner বাদ দিয়ে আসল error line-টা ইউজার-ফেসিং মেসেজে দেখায়
  (যেমন `ffmpeg error: -to value smaller than -ss; aborting`)।
- **Tests**: `pipeline/tests/test_cascade_crash_regression.py` রিপোর্ট করা
  প্যাটার্ন (১০০s-span entry + ২৭ collision entry) subtitle_builder →
  edit_guideline → auto_cut (ffmpeg mocked) দিয়ে চালিয়ে assert করে —
  (ক) subtitle_builder আউটপুটে কোনো zero/negative-duration নেই, (খ) auto_cut
  exception ছোড়ে না, (গ) job `status: "ok"`-এ শেষ হয়। মোট ৩৮২ টেস্ট OK।

বাকি কাজ:
- ব্যবহারকারীর নিজের real-media QA রান:
  - docs/FINAL_SUMMARY.md → "Subtitle QA Fixes (A1-E4)" → "The user must do this"
  - যে jobগুলো আগে cascade-crash/zero-duration দিয়েছিল (যেমন
    `97a9b90e-...`) সেগুলো রি-রান করে confirm করো যে এখন final `.srt`-এ আর
    zero/negative duration নেই এবং draft render-এর সময় কোনো ffmpeg
    abort/crash নেই।
- `expected_duration_sec`-এর semantics মাথায় রেখে (draft ≈ source
  duration) যে job-গুলো `user_upload` দিয়ে আবার রান করবে সেগুলোর draft
  validation এখন হিউম্যান-রেকর্ডিংয়ের প্রাকৃতিক pacing variance গ্রহণ করে।
