# HANDOFF NEXT

গ্রুপ A-E (subtitle-QA-fix, A1-E4) + E5 (real-media QA-writeback fix) +
E6 (draft duration-validation fix) + E7 (cascade-crash fix) +
E8 (user_upload duration-check removal) + E9 (duration-drift fix) + E10
(test-isolation fix) + F8 (Whisper timing authority) সম্পূর্ণ।

F8 (`whisper timing authority`): Whisper এখন টাইমিং-এর প্রাইমারি অথোরিটি

- **F1-F3 (Chinese subtitle extraction)**: প্রতিটা চাঙ্কের Gemini extraction-এর
  পরে চাঙ্ক-অডিও `-vn -ar 16000 -ac 1` দিয়ে বের করে Whisper দিয়ে
  transcribe করা হয় (per-chunk, `_dedup_merge`-এর আগে; chunking/dedup
  কাঠামো অপরিবর্তিত)। প্রতিটা Whisper segment একটা subtitle entry হয়
  (টাইমিং Whisper-এর); Gemini-এর text তখনই ব্যবহার হয় যখন কোনো unused Gemini
  line overlap_ratio ≥ 0.5 (SUBTITLE_OVERLAP_MATCH_MIN) **এবং** text
  similarity ≥ 0.3 — তখন `text_source="gemini_cleaned"`, নাহলে
  `text_source="whisper_raw"`। কোনো Whisper segment-এর সঙ্গে zero-overlap
  Gemini line drop হয় + `gemini_hallucinated_dropped`-এ গোনা হয়। Whisper
  unavailable/falsy হলে আজকের pure-Gemini আউটপুট অপরিবর্তিত (text_source নেই)।
  F7-এর `timing_source` বাদ → `text_source`।
- **D3 (user-uploaded voiceover)**: Whisper-primary — voiceover-টা
  unconditionally transcribe (`language="hi"`), serial-গুলো sequential fuzzy
  match (matched → `alignment_source="whisper"`)। Unmatched serial-গুলো
  **bounded Gemini secondary pass**-এ যায়: Gemini শুধু ওই serial-গুলো দেখে, আর
  একটা item তখনই মানা হয় যখন `end_sec <= last_speech_end +
  WHISPER_TAIL_TOLERANCE_SEC (1.0)` (`alignment_source="gemini_assisted"`) —
  Gemini কখনো Whisper-এর ধরা অডিওর বাইরে টাইম দিতে পারে না। নতুন status
  `"gemini_assisted"`; `"ok"` = সব serial Whisper-matched (বা pure-Gemini
  fallback-এ সব Gemini-matched); `"whisper"` = কিছু line match হয়নি / Gemini
  সাহায্য করেনি; `"equal_split"` আগের মতো। Whisper unavailable হলে আজকের
  pure-Gemini flow অপরিবর্তিত (Gemini → equal-split)। E9
  `_clamp_timestamps_to_audio` গার্ড প্রতিটা পাথে আগের মতোই চলে।
- **Config**: `WHISPER_MODEL` "base" → **"small"** (প্রাইমারি টাইমিং-এর জন্য
  "base"-এর boundary ঢিলা), নতুন `WHISPER_MODEL_ZH`/`WHISPER_MODEL_HI` (None),
  `SUBTITLE_OVERLAP_MATCH_MIN` (0.5), `WHISPER_TAIL_TOLERANCE_SEC` (1.0)।
- **Target-collapse render fix (F7 item 4)**: near-zero target এখন near-zero
  render করে, full source clip নয়।
  - `edit_guideline._build_entry`: collapsed target (≤ 0) + healthy source →
    `pts_multiplier = RENDER_MIN_SEGMENT_DURATION_SEC / source_duration` (আগে
    1.0), এখনো `invalid_duration` flag।
  - `auto_cut.py`: degenerate-segment guard এখন target-side-ও trigger করে
    (`target_duration <= RENDER_MIN_SEGMENT_DURATION_SEC` + normal-length
    source → minimal window কাটা হয়)। `extreme_speed_ratio` এখনো
    soft/informational।
- **New shared module `pipeline/whisper_align.py`** (F7 item 1, extended):
  `transcribe_segments`, `transcribe_words`, `last_speech_end`,
  `overlap_ratio`, `match_words_to_entries` (voiceover_upload-এর প্রাইভেট
  `_transcribe_words`/`_match_words_to_entries` এখন alias; call site ও mocks
  অপরিবর্তিত)। সব helper কখনো raise করে না।
- **Tests**: full suite **৪৩০ টেস্ট OK + ৪২ subtest** (was ৪০৫; +২৫) —
  test_whisper_align.py (নতুন), whisper-primary subtitle_extract
  (text_source, hallucinated drop, per-chunk merge), D3 gemini_assisted
  (speech-tail-এর ভিতরে/বাইরে), `max(end_sec) <= audio + epsilon` +
  E9 clamp whisper-primary পাথেও, target-collapse render (edit_guideline /
  auto_cut)।

বাকি কাজ:
- ব্যবহারকারীর নিজের real-media QA রান:
  - docs/FINAL_SUMMARY.md → "Subtitle QA Fixes (A1-E4)" → "The user must do this"
  - Whisper-primary পাথ (যখন Whisper ইনস্টল করা থাকবে) দিয়ে একটা job রি-রান
    করে confirm করো যে subtitles-এর টাইমিং Whisper-এর সাথে মিলছে, `text_source`
    ফিল্ড ঠিকমতো আসছে, এবং `max(end_sec)` কোনো ফাইলে probed audio-দৈর্ঘ্যের
    বাইরে যাচ্ছে না।
- Whisper এখনো এনভায়রনমেন্টে ইনস্টল নেই (pip `openai-whisper` + `numpy`) —
  ইনস্টল করলে F1-F3/D3-এর প্রাইমারি পাথ সক্রিয় হবে; না করলে pure-Gemini
  fallback-এ সব আগের মতোই চলে (সম্পূর্ণ resilience সংরক্ষিত)।
- E6-এর `expected_duration_sec` (draft ≈ source duration) ধারণাটা এখন শুধু
  auto-TTS পাথেই প্রযোজ্য; `user_upload` পাথে draft-এর মোট দৈর্ঘ্য source-এর
  চেয়ে আলাদা হওয়াই প্রত্যাশিত (কিন্তু ভয়েসওভার অডিও দৈর্ঘ্যের সমান থাকবে)।
