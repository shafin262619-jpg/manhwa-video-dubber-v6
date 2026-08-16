# HANDOFF NEXT

গ্রুপ A-E (subtitle-QA-fix, A1-E4) + E5 (real-media QA-writeback fix) +
E6 (draft duration-validation fix) + E7 (cascade-crash fix) +
E8 (user_upload duration-check removal) + E9 (duration-drift fix) সম্পূর্ণ।

E9 (`manhwa-video-dubber-v6-duration-drift-fix`): user_upload ডিউরেশন-ড্রিফট বাগ

- **বাগ**: real-media QA job `6b2c0929-607f-4f79-a99a-76e0ed0dd5f1`-এ ৫২২s
  ভয়েসওভার অডিও থেকে **৭৯৭.৮s** ফাইনাল ভিডিও রেন্ডার হয়েছিল (~৫৩% বেশি,
  ২২৬-এর মধ্যে ১১১ segment `extreme_speed_ratio`)।
- **রুট-কজ (কোনো cap ছিল না)**: `pts_multiplier`-এর উপর কোনো ক্যাপ/ক্ল্যাম্প
  ছিল না — `edit_guideline.py` আসল মান রাখে (`target_duration / source_duration`),
  `auto_cut.py` সেটা সরাসরি `setpts=<multiplier>*PTS`-এ দেয়; `SPEED_RATIO_MIN/MAX`
  (0.5/2.0) শুধু QA-flag। E7-এর `_redistribute_collision_cluster` +
  `SUBTITLE_MIN_SERIAL_DURATION_SEC` এবং `RENDER_MIN_SEGMENT_DURATION_SEC`
  min-window সবই source-side — অডিও-সাইড target-কে ছোঁয় না।
- **আসল কারণ**: D3 alignment (Gemini/Whisper) প্রতিটা serial-এর জন্য আসল
  অডিও দৈর্ঘ্যের (৫২২s) বাইরে end-time দিচ্ছিল, ফলে target duration-গুলোর
  যোগফল অডিওর চেয়ে বেশি হচ্ছিল এবং E2 প্রতিটা clip-কে স্ফীত target-এ
  stretch করে ভিডিও লম্বা করে দিচ্ছিল।
- **ফিক্স**: `_clamp_timestamps_to_audio` (এখন `voiceover_unify.py`-তে, D3
  ইমপোর্ট করে) সব alignment-টাইমস্ট্যাম্পকে `[0, total_sec]`-এর মধ্যে
  ক্ল্যাম্প করে + কনসিকিউটিভ start-কে আগের end-এ টেনে আনে — target duration
  কখনো অডিওর চেয়ে বেশি যোগফল দিতে পারে না। D3-তে (প্রাইমারি) + D4-তে
  (user_upload পাথে defense-in-depth) দুই জায়গাতেই প্রযোজ্য। ফলাফলে এখন
  `target_total_sec` + `clamped_serials` থাকে, ক্ল্যাম্প হলে non-blocking
  warning আসে। `auto_tts` পাথ অপরিবর্তিত।
- **Tests**: full suite **৪০৩ টেস্ট OK** (was ৩৯৫; +৮) — 522s/797.8s
  real-media প্যাটার্ন রিপ্লিকেট করা টেস্ট, 20x+ multiplier uncapped
  (শুধু flagged), D4 ক্ল্যাম্প-গার্ড, draft == voiceover দৈর্ঘ্য।

টেস্ট-আইসোলেশন ফিক্স (E9 audit-এর সময় পাওয়া):
- `test_video_ingest`-এর `/upload` টেস্টটি FA-C1 বেহেভিয়রের কারণে ব্যাকগ্রাউন্ড
  ডেমন থ্রেডে auto-full-render চালু করে (app.py: `/upload` ডিফল্ট `auto_tts` +
  একই থ্রেডে `_run_auto_full_render`)। টেস্ট শুধু `upload_pipeline` stage-এর
  "done" পর্যন্ত অপেক্ষা করত, তাই থ্রেডটি টেস্ট শেষ হওয়ার পরেও বেঁচে থেকে
  পরবর্তী টেস্টে লিক করত — `test_voiceover_auto`-এর `_call_tts` mock-কে কল করে
  `fake.assert_not_called()` ফেল করত (সিরিয়াল রানে flaky failure)।
- ফিক্স: টেস্টে `full_auto_chain.run_auto_tts_chain` mock করা + `_wait_for_stage_done`
  দিয়ে `auto_full_render` stage settle হওয়া পর্যন্ত অপেক্ষা — থ্রেড টেস্টের
  `with` ব্লক বন্ধ হওয়ার আগেই পুরোপুরি শেষ হয়, নেটওয়ার্ক কলও হয় না।

বাকি কাজ:
- ব্যবহারকারীর নিজের real-media QA রান:
  - docs/FINAL_SUMMARY.md → "Subtitle QA Fixes (A1-E4)" → "The user must do this"
  - যে jobগুলো আগে cascade-crash/zero-duration/duration-drift দিয়েছিল
    (যেমন `6b2c0929-607f-4f79-a99a-76e0ed0dd5f1`, `97a9b90e-...`) সেগুলো
    রি-রান করে confirm করো যে এখন ফাইনাল ভিডিওর দৈর্ঘ্য == ভয়েসওভার অডিওর
    দৈর্ঘ্য, `.srt`-এ আর zero/negative duration নেই এবং কোনো ffmpeg
    abort/crash নেই।
- E6-এর `expected_duration_sec` (draft ≈ source duration) ধারণাটা এখন শুধু
  auto-TTS পাথেই প্রযোজ্য; `user_upload` পাথে draft-এর মোট দৈর্ঘ্য source-এর
  চেয়ে আলাদা হওয়াই প্রত্যাশিত (কিন্তু ভয়েসওভার অডিও দৈর্ঘ্যের সমান থাকবে)।
