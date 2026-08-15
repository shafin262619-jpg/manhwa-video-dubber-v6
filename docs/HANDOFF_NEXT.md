# HANDOFF NEXT

গ্রুপ A-E (subtitle-QA-fix, A1-E4) + E5 (real-media QA-writeback fix) +
E6 (draft duration-validation fix) + E7 (cascade-crash fix) +
E8 (user_upload duration-check removal) সম্পূর্ণ।

E8 (`manhwa-video-dubber-v6-duration-check-removed`): E6-এর ডিজাইন সংশোধন —

- **E6-এর মূল ভিত্তিই ভুল ছিল**: draft validation-এ `user_upload` অডিওর মোট
  দৈর্ঘ্য source ভিডিওর মোট দৈর্ঘ্যের সাথে tolerance-ভিত্তিক তুলনা করে ব্লক
  করা হচ্ছিল। কিন্তু পাইপলাইনের উদ্দেশ্যই হলো প্রতিটা scene-clip-কে সেই
  সেগমেন্টের ভয়েসওভারের দৈর্ঘ্যের সাথে মেলানো (pts stretch); অনুবাদের কারণে
  দৈর্ঘ্য কম/বেশি হওয়া সম্পূর্ণ স্বাভাবিক (real-media টেস্ট: ৩০৩s ভিডিওতে
  ৫২৩s অডিও — বৈধ)। মোট দৈর্ঘ্যের পার্থক্যে আর কখনো প্রসেসিং ব্লক হয় না।
- **আসল সঠিকতা-চেক = per-segment alignment**: `voiceover_unify.py`-এর D4 স্টেপ
  এখন প্রতিটা সাবটাইটেল serial-এর জন্য voiceover timestamp থাকাটা যাচাই করে;
  কোনো segment-এর অডিও না পেলে `VoiceoverAlignmentError`-এ ব্লক। এছাড়া
  `align_uploaded_voiceover` অডিওর measurable content না থাকলে ব্লক করে।
- **Optional non-blocking warning**: draft দৈর্ঘ্য source-এর ৫x+/১/৫x-
  থেকে চরমভাবে আলাদা হলে ফলাফল পেজে সতর্কতা-ব্যানার দেখায় ("ঠিক ফাইল
  আপলোড হয়েছে তো?") — কখনো ব্লক নয়।
- **Tests**: full suite **৩৯৫ টেস্ট OK** (was ৩৮২; +১৩) — user_upload পাথে
  ৫০%+ লম্বা/ছোট অডিও সফলভাবে এগিয়ে যায়, শুধু per-segment alignment
  failure-এই ব্লক হয়।

বাকি কাজ:
- ব্যবহারকারীর নিজের real-media QA রান:
  - docs/FINAL_SUMMARY.md → "Subtitle QA Fixes (A1-E4)" → "The user must do this"
  - যে jobগুলো আগে cascade-crash/zero-duration দিয়েছিল (যেমন
    `97a9b90e-...`) সেগুলো রি-রান করে confirm করো যে এখন final `.srt`-এ আর
    zero/negative duration নেই এবং draft render-এর সময় কোনো ffmpeg
    abort/crash নেই।
- E6-এর `expected_duration_sec` (draft ≈ source duration) ধারণাটা এখন শুধু
  auto-TTS পাথেই প্রযোজ্য; `user_upload` পাথে draft-এর মোট দৈর্ঘ্য source-এর
  চেয়ে আলাদা হওয়াই প্রত্যাশিত।
