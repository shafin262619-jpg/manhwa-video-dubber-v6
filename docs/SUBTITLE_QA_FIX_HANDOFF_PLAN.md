# manhwa-video-dubber-v6 — Subtitle QA ফিক্স: GitHub-ভিত্তিক Multi-Agent Handoff প্ল্যান

**এটা কী:** এই ডকুমেন্টটা `manhwa-video-dubber-v6` রিপোজিটরির (ট্যাগ
`manhwa-video-dubber-v6-full-auto-final` পর্যন্ত সম্পূর্ণ, ২৯০টা টেস্ট
পাশ) subtitle-extraction pipeline-এ পাওয়া ৬টা রুট-কজ বাগের জন্য একটা
সম্পূর্ণ, ছোট-ছোট-চাংক GitHub-এজেন্ট প্ল্যান। এই ৬টা বাগ আগের কথোপকথনে
`subtitles_hi.srt` আর Turboscribe ট্রান্সক্রিপ্ট মিলিয়ে বের করা হয়েছিল:

1. Zero-duration / duplicate-timestamp entry-র cluster (৩.০০০s-এ ৮১টা,
   ৪.০০০s-এ ৪৮টা, ৫.০০০s-এ ৪৩টা লাইন আটকে থাকা) — কোনো coverage-gap বা
   duplicate-cluster ডিটেকশন নেই।
2. একটা বড় dialogue-dense ব্লক (~৫০ সেকেন্ড / ৩৭ লাইন) পুরোপুরি বাদ পড়া,
   আর কয়েকটা লাইন ভুল জায়গায় বসে যাওয়া — কারণ পুরো ভিডিও একবারেই একটা
   Gemini কলে পাঠানো হয় (`LONG_VIDEO_CHUNK_THRESHOLD_SEC = 600`s, অথচ
   ভিডিও মাত্র ~৫-৬ মিনিট)।
3. কোনো targeted re-extraction/repair mechanism নেই — flag হওয়া অংশ
   এমনিই srt/render পর্যন্ত চলে যায়।
4. `_serialize()`-এ শুধু forward-overlap ক্ল্যাম্প লগ হয়, zero-duration
   বা বড় backward jump কিছুই লগ হয় না।
5. Extraction-এর কোনো independent cross-check নেই (audio-based Whisper
   pass দিয়ে rough verify করার মতো কিছু নেই)।
6. ভয়েসওভার আপলোড/রেকর্ডিং শুরুর আগে ব্যবহারকারীকে কোনো coverage/QA
   রিপোর্ট দেখানো হয় না — সমস্যাটা এখন পুরো voiceover রেকর্ড করার পরেই
   ধরা পড়ে।

**এই প্ল্যানের ভিত্তি:** এটা টেমপ্লেট না — আমি সত্যিকারের zip খুলে
`pipeline/subtitle_extract.py`, `pipeline/subtitle_builder.py`,
`pipeline/translator.py`, `pipeline/voiceover_upload.py`,
`pipeline/job_status.py`, `pipeline/config.py`, `app.py` আর
`pipeline/tests/`-এর বিদ্যমান কনভেনশন পড়ে প্রতিটা চাংক-প্রম্পট বানিয়েছি।
নিচের প্রতিটা প্রম্পটে ফাইল/ফাংশন নাম, config constant, আর টেস্ট-প্যাটার্ন
— সবই এই রিপোর সত্যিকারের কোড থেকে নেওয়া।

---

## Monkey AI বনাম Claude

- **Monkey AI:** আপনার GitHub আগে থেকেই বাইন্ড করা আছে (আগের FA-সিরিজ
  চাংকগুলো এভাবেই push হয়েছে) — নিচের প্রতিটা প্রম্পট সরাসরি পেস্ট করলেই
  কাজ করা উচিত।
- **Claude:** এই ধরনের কাজের জন্য সবচেয়ে উপযুক্ত টুল **Claude Code**
  (টার্মিনাল/ডেস্কটপ/মোবাইল) — real `git clone`/`push` করতে পারে। plain
  claude.ai chat-এর built-in code-sandbox-এ সাধারণত ইন্টারনেট বন্ধ থাকে।
  Claude Code-এর GitHub Actions integration (`/install-github-app`,
  `@claude` মেনশন) বেশি অটোমেটেড কিন্তু আলাদা API billing লাগতে পারে —
  আপ-টু-ডেট শর্তের জন্য code.claude.com/docs/en/github-actions চেক করুন।

---

## রিপো — ধাপ ০ স্কিপ (আগে থেকেই তৈরি)

রিপো, `docs/` ফোল্ডার, `.gitignore`, tag/changelog/handoff কনভেনশন — সব
আগে থেকেই আছে:

```
Repo: https://github.com/shafin262619-jpg/manhwa-video-dubber-v6.git  (branch: main)
শেষ ট্যাগ: manhwa-video-dubber-v6-full-auto-final  (290 tests, 100% pass)
```

শুধু একটা কাজ ম্যানুয়ালি করে নিন — এই প্ল্যান ফাইলটা (এই পুরো ডকুমেন্ট)
ডাউনলোড করে repo-র `docs/SUBTITLE_QA_FIX_HANDOFF_PLAN.md` নামে সেভ করে
কমিট করুন, যাতে প্রতিটা চাংক নিজে থেকেই এটা পড়তে পারে:

```bash
cd manhwa-video-dubber-v6
git pull origin main
git tag -l | grep full-auto-final   # ভেরিফাই করুন tag আছে
cp ~/Downloads/SUBTITLE_QA_FIX_HANDOFF_PLAN.md docs/
git add docs/SUBTITLE_QA_FIX_HANDOFF_PLAN.md
git commit -m "docs: add subtitle-QA-fix handoff plan (chunks A1-E4)"
git push origin main
```

## Repo কনভেনশন (অপরিবর্তিত, আগের মতোই)

- branch সবসময় `main`।
- প্রতিটা চাংকের শেষে ট্যাগ: `chunk-<id>-done` (যেমন `chunk-A1-done`),
  একদম শেষে `manhwa-video-dubber-v6-qa-final`।
- `docs/HANDOFF_NEXT.md` — **ওভাররাইট** (বর্তমান অবস্থা)।
- `docs/CHANGELOG.md` — **append-only** (নতুন এন্ট্রি নিচে যোগ করুন, পুরনো
  এন্ট্রি স্পর্শ করবেন না)।
- টেস্ট রান: `python3 -m unittest discover -s pipeline/tests -v`
  (বর্তমানে ২৯০টা পাশ করছে — এই সংখ্যাটা প্রতিটা চাংকের শেষে বাড়বে,
  কখনো কমবে না)।
- `.gitignore` ইতিমধ্যে `uploads/`, `*.mp4` ইত্যাদি বাদ দিচ্ছে — নতুন কিছু
  যোগ করার দরকার নেই যদি না নতুন টেম্প ফোল্ডার/ফাইল-টাইপ তৈরি হয়
  (যেমন গ্রুপ B-এর repair segment ক্লিপ — নিচে B1-এ উল্লেখ আছে)।

---

## বড়-ছবি ম্যাপিং টেবিল

| গ্রুপ | স্কোপ | ফাইল | সাব-চাংক |
|---|---|---|---|
| **A** | Coverage-gap + duplicate/degenerate-timestamp ডিটেকশন (বিশুদ্ধ Python) | `pipeline/subtitle_builder.py` | **A1, A2, A3** |
| **B** | Flag হওয়া রেঞ্জের জন্য targeted re-extraction repair (external Gemini API, সবচেয়ে ঝুঁকিপূর্ণ) | `pipeline/subtitle_extract.py`, `pipeline/subtitle_builder.py`, `app.py` | **B1, B2, B3, B4** |
| **C** | ছোট/dialogue-dense ভিডিওতেও sub-chunking থ্রেশহোল্ড কমানো | `pipeline/config.py` | **C1, C2** |
| **D** | Independent local-Whisper cross-check verification pass | `pipeline/subtitle_verify.py` (নতুন), `app.py` | **D1, D2, D3** |
| **E** | User-facing QA-summary + `app.py` wiring + regression + final wrap-up | `pipeline/subtitle_qa.py` (নতুন), `app.py`, `docs/` | **E1, E2, E3, E4** |

মোট **১৬টা প্রম্পট**, ধারাবাহিকভাবে A1→A2→A3→B1→B2→B3→B4→C1→C2→D1→D2→D3→E1→E2→E3→E4।
গ্রুপ A-এর diagnostics গ্রুপ B খায় (repair কোন রেঞ্জে চালাতে হবে সেটা
A বলে দেয়), তাই A অবশ্যই B-এর আগে। C স্বাধীন কিন্তু B-এর পরে রাখা হয়েছে
যাতে repair mechanism আগে স্থিতিশীল হয়ে যায়। D স্বাধীন। E সবকিছু একসাথে
করে ইউজারকে দেখায়, তাই সবার শেষে।

**গুরুত্বপূর্ণ:** A1/A2-এর পরে diagnostics শুধু লেখা হবে, এখনো কোনো
auto-repair বা UI নেই — এটা স্বাভাবিক, B/D/E-এ ধাপে ধাপে যোগ হবে।

---

## গ্রুপ A — Coverage-gap + Duplicate-cluster ডিটেকশন

### A1 — Coverage-gap ডিটেকশন

```
আমি manhwa-video-dubber-v6-এর subtitle-QA-fix আপডেটে কাজ করছি, GitHub-ভিত্তিক
চেইনের "চাংক A1" (baseline: tag manhwa-video-dubber-v6-full-auto-final,
২৯০টা টেস্ট পাশ)।

Repo: https://github.com/shafin262619-jpg/manhwa-video-dubber-v6.git  (branch: main)

প্রথমে করো:
1. Repo clone/pull করো, tag manhwa-video-dubber-v6-full-auto-final থেকে
   ভেরিফাই করো (`git tag -l`)।
2. docs/HANDOFF_NEXT.md পড়ো।
3. docs/SUBTITLE_QA_FIX_HANDOFF_PLAN.md-এর ভূমিকা + "বড়-ছবি ম্যাপিং
   টেবিল" অংশ পড়ো (পুরো ৬-বাগ প্রেক্ষাপট বোঝার জন্য)।
4. pipeline/subtitle_builder.py পুরোটা পড়ো (ছোট ফাইল, ~১৫০ লাইন) —
   বিশেষ করে `_serialize()` আর `build_subtitle_list()`।

তোমার স্কোপ:

`pipeline/subtitle_builder.py`-তে একটা নতুন ফাংশন যোগ করো:

```python
def detect_gaps(serialized_entries, threshold_sec=None):
    """Serialized (post-_serialize) এন্ট্রির consecutive জোড়ার মধ্যে gap বের করে।

    threshold_sec None হলে config.SUBTITLE_GAP_FLAG_THRESHOLD_SEC ব্যবহার করো
    (এই চাংকেই config.py-তে নতুন যোগ করো, ডিফল্ট 6.0)।

    প্রতিটা gap-এর জন্য (next.start_sec - prev.end_sec > threshold_sec):
        {"after_serial": prev["serial"], "before_serial": next["serial"],
         "gap_start_sec": prev["end_sec"], "gap_end_sec": next["start_sec"],
         "gap_sec": round(next["start_sec"] - prev["end_sec"], 3)}
    রিটার্ন করো লিস্ট, chronological order-এ। "status": "extraction_failed"
    এন্ট্রি gap-চেকে অংশ নেবে (এদের নিজেদের মধ্যেও gap হতে পারে) — শুধু
    এন্ট্রি-বাই-এন্ট্রি consecutive gap দেখো, ফিল্টার করার দরকার নেই।
```

hard constraint: এই ফাংশন pure Python, কোনো নতুন network/external call
না, কোনো side-effect (file write) না — শুধু ইনপুট লিস্ট নিয়ে আউটপুট
লিস্ট রিটার্ন করবে। `build_subtitle_list()`-এ এখনই কল/wire করার দরকার
নেই — সেটা A3-এর স্কোপ।

`config.py`-তে নতুন যোগ করো:
```python
# Consecutive serialized subtitle entries whose gap exceeds this (seconds)
# are flagged as possible missing content (QA diagnostics, A1).
SUBTITLE_GAP_FLAG_THRESHOLD_SEC = 6.0
```

টেস্ট: `pipeline/tests/test_subtitle_builder.py`-তে বিদ্যমান
`SubtitleBuilderBase`-এর মতো fixture ব্যবহার করে নতুন টেস্ট-কেস যোগ করো
(নতুন `TestCase` ক্লাস বা বিদ্যমান ক্লাসে মেথড, যেটা বিদ্যমান স্টাইলের
সাথে বেশি মেলে সেটা বেছে নাও):
- কোনো gap নেই (consecutive এন্ট্রি কাছাকাছি) → খালি লিস্ট।
- একটা বড় gap (যেমন threshold-এর ওপরে) → ঠিক ১টা flagged dict, সঠিক
  serial/gap_sec সহ।
- threshold-এর নিচের ছোট gap → flag হবে না (boundary-এর ঠিক ওপরে/নিচে
  উভয় কেস টেস্ট করো)।
- একাধিক gap থাকলে → chronological order-এ সবগুলো রিটার্ন হয়।
- custom threshold_sec প্যারামিটার override কাজ করে।

শেষে (Definition of Done):
1. পুরো test suite পাশ করছে কনফার্ম করো:
   `python3 -m unittest discover -s pipeline/tests -v`
2. docs/HANDOFF_NEXT.md ওভাররাইট করো: "চাংক A1 (detect_gaps) সম্পূর্ণ,
   standalone, wire হয়নি এখনো। পরের কাজ: A2 (duplicate-cluster
   detection + _serialize() logging fix)।"
3. docs/CHANGELOG.md-এ নতুন এন্ট্রি যোগ করো (নতুন ফাংশন + নতুন config
   constant + নতুন টেস্ট কাউন্ট)।
4. Commit: "chunk A1: subtitle_builder.detect_gaps() coverage-gap diagnostics"
   — push করো।
5. Tag: git tag chunk-A1-done && git push origin chunk-A1-done

context ফুরিয়ে গেলেও এই ৫টা ধাপ (টেস্ট, HANDOFF_NEXT.md, CHANGELOG.md,
commit, tag) অবশ্যই শেষ করো, কোন অংশ বাকি স্পষ্ট লিখে।
```

### A2 — Duplicate/degenerate-timestamp cluster ডিটেকশন + `_serialize()` logging fix

```
আমি manhwa-video-dubber-v6-এর subtitle-QA-fix আপডেটে কাজ করছি, GitHub-ভিত্তিক
চেইনের "চাংক A2" (A1 শেষ)।

Repo: https://github.com/shafin262619-jpg/manhwa-video-dubber-v6.git  (branch: main)

প্রথমে করো:
1. Repo clone/pull করো, tag chunk-A1-done থেকে ভেরিফাই করো।
2. docs/HANDOFF_NEXT.md পড়ো।
3. pipeline/subtitle_builder.py-তে A1-এ যোগ হওয়া `detect_gaps()` আর
   `_serialize()` পড়ো।

তোমার স্কোপ:

**১. Duplicate/degenerate cluster ডিটেকশন** — `pipeline/subtitle_builder.py`-তে:

```python
def detect_duplicate_clusters(serialized_entries, min_count=None):
    """post-_serialize এন্ট্রিতে consecutive রান খোঁজে যাদের start_sec অভিন্ন
    (রাউন্ডেড মান, যেহেতু _serialize() already round(x, 3) করে রাখে), অথবা
    start_sec == end_sec (zero-duration)।

    min_count None হলে config.SUBTITLE_DUP_CLUSTER_MIN_COUNT ব্যবহার করো
    (এই চাংকেই config.py-তে যোগ করো, ডিফল্ট 3) — অর্থাৎ ৩+ consecutive
    এন্ট্রি একই start_sec শেয়ার করলে বা zero-duration হলে সেটা একটা
    cluster হিসেবে গণ্য।

    প্রতিটা cluster:
        {"start_serial": ..., "end_serial": ..., "start_sec": ...,
         "count": ..., "reason": "same_start_timestamp" | "zero_duration"}
    দুটো কারণ একসাথে থাকতে পারে এমন রান হলে "zero_duration" প্রাধান্য পাবে
    (যেহেতু সেটা বেশি severe)। রিটার্ন করো লিস্ট, serial-অর্ডারে।
    """
```

hard constraint: আগের মতোই pure Python, side-effect নেই, এখনই wire করার
দরকার নেই (A3-এর কাজ)।

**২. `_serialize()`-এর logging fix** — বর্তমানে শুধু forward-overlap
ক্ল্যাম্প হলে `logger.warning` হয়। এর সাথে যোগ করো (একই ফাংশনের ভেতরে,
per-entry লুপে, ওভারল্যাপ-ক্ল্যাম্প লজিকের কাছেই):
- `end < start` (এখন silently `end = start` করে দেয়) হলে একটা আলাদা
  `logger.warning("subtitle serial %d zero/negative duration after clamp "
  "(start %.3fs, original end %.3fs)", index, start, entry["end_sec"])`
  যোগ করো — বিদ্যমান behavior (end = start) বদলাবে না, শুধু log নতুন।
- `start_sec == end_sec` ইনপুটে (raw entry-তেই zero-duration, ক্ল্যাম্প
  ছাড়াই) থাকলেও একই রকম distinct warning দাও, যাতে "১৭২টা এন্ট্রি
  ০.০০০s duration-এর" মতো কেস pipeline.log-এ সরাসরি দেখা যায়।

hard constraint: বিদ্যমান overlap-clamp warning message/behavior বদলানো
যাবে না (backward-compat) — শুধু নতুন warning লাইন যোগ করা।

`config.py`-তে যোগ করো:
```python
# 3+ consecutive serialized subtitle entries sharing the same start_sec (or
# zero-duration) are flagged as a degenerate extraction cluster (QA
# diagnostics, A2).
SUBTITLE_DUP_CLUSTER_MIN_COUNT = 3
```

টেস্ট (`pipeline/tests/test_subtitle_builder.py`):
- `detect_duplicate_clusters()`: কোনো cluster নেই কেস; একটা same-start
  cluster (৩+ এন্ট্রি) সঠিকভাবে ধরা পড়ে; একটা zero-duration cluster
  আলাদা reason নিয়ে ধরা পড়ে; min_count-এর নিচের রান (২টা) flag হয় না;
  একাধিক cluster chronological order-এ রিটার্ন হয়; custom min_count
  override কাজ করে।
- `_serialize()` logging: `assertLogs` ব্যবহার করে zero-duration ইনপুট
  আর clamp-induced zero-duration উভয় কেসে নতুন warning লগ হচ্ছে
  ভেরিফাই করো, আর বিদ্যমান overlap-clamp টেস্ট এখনো পাশ করছে কনফার্ম করো
  (regression)।

শেষে (Definition of Done):
1. পুরো test suite পাশ করছে কনফার্ম করো:
   `python3 -m unittest discover -s pipeline/tests -v`
2. docs/HANDOFF_NEXT.md: "A2 (detect_duplicate_clusters + logging fix)
   সম্পূর্ণ। পরের কাজ: A3 (build_subtitle_list()-এ diagnostics wire করে
   subtitle_qa.json লেখা)।"
3. docs/CHANGELOG.md-এ এন্ট্রি যোগ করো।
4. Commit: "chunk A2: duplicate/degenerate-timestamp cluster detection + _serialize() zero-duration logging" — push করো।
5. Tag: git tag chunk-A2-done && git push origin chunk-A2-done
```

### A3 — `build_subtitle_list()`-এ diagnostics wire করা + `subtitle_qa.json`

```
আমি manhwa-video-dubber-v6-এর subtitle-QA-fix আপডেটে কাজ করছি, GitHub-ভিত্তিক
চেইনের "চাংক A3" (A2 শেষ, গ্রুপ A-এর শেষ সাব-চাংক)।

Repo: https://github.com/shafin262619-jpg/manhwa-video-dubber-v6.git  (branch: main)

প্রথমে করো:
1. Repo clone/pull করো, tag chunk-A2-done থেকে ভেরিফাই করো।
2. docs/HANDOFF_NEXT.md পড়ো।
3. `build_subtitle_list()` (pipeline/subtitle_builder.py) আর app.py-র
   `_run_upload_pipeline()`-এ এটা কীভাবে কল হয় দেখে বোঝো (app.py-তে
   `subtitle_builder.build_subtitle_list(job_id)` — return value ব্যবহার
   হয় না, `translator.translate_subtitles()`-এর জন্য পরে ফাইল থেকেই
   `subtitles_zh.json` পড়া হয়)।

তোমার স্কোপ:

`build_subtitle_list()`-এর ভেতরে, `result = _serialize(entries)`-এর পরে
আর ফাইল লেখার আগে/পরে:

1. `detect_gaps(result)` আর `detect_duplicate_clusters(result)` কল করো।
2. মোট coverage হিসাব করো: `covered_sec = duration - sum(g["gap_sec"] for g in gaps)`
   (নেগেটিভ হলে 0.0 এ ক্ল্যাম্প করো)।
3. একটা diagnostics dict বানাও:
   ```python
   {
       "job_id": job_id,
       "total_duration_sec": round(duration, 3),
       "covered_duration_sec": round(covered_sec, 3),
       "entries_count": len(result),
       "gaps": gaps,
       "duplicate_clusters": duplicate_clusters,
   }
   ```
4. এটা `job_dir / "subtitle_qa.json"` এ লেখো (`subtitles_zh.json`-এর
   পাশে, একই `json.dumps(..., ensure_ascii=False, indent=2)` স্টাইলে)।

hard constraint (backward compat): `build_subtitle_list()`-এর **রিটার্ন
ভ্যালু অপরিবর্তিত** থাকতে হবে — এখনো শুধু `result` (serialized entries
list) রিটার্ন করবে, dict না। যারা এই ফাংশন কল করে (app.py, বিদ্যমান
টেস্ট) কারো কোনো বদল লাগবে না — diagnostics শুধু একটা সাইড-আর্টিফ্যাক্ট
(`subtitle_qa.json`) হিসেবে লেখা হচ্ছে।

একটা ছোট হেল্পার ফাংশনও যোগ করো, পরের গ্রুপগুলো (B, E) ব্যবহার করবে:
```python
def load_subtitle_qa(job_id, upload_root=None):
    """subtitle_qa.json পড়ে dict রিটার্ন করে; ফাইল না থাকলে/malformed হলে
    {"gaps": [], "duplicate_clusters": [], ...} ডিফল্ট রিটার্ন করে, কখনো
    raise করে না।"""
```

টেস্ট: `build_subtitle_list()`-এর বিদ্যমান টেস্টগুলো (কাউন্ট/অর্ডার/ক্ল্যাম্প
আচরণ) অপরিবর্তিত থাকা কনফার্ম করো (regression)। নতুন টেস্ট যোগ করো:
- gap/cluster-যুক্ত fixture দিয়ে `subtitle_qa.json` সঠিক কনটেন্ট নিয়ে
  লেখা হয়, `build_subtitle_list()`-এর return value আগের মতোই থাকে।
- gap/cluster ছাড়া clean fixture দিয়ে `subtitle_qa.json`-এ খালি লিস্ট।
- `load_subtitle_qa()`: ফাইল থাকলে সঠিক পড়া, ফাইল না থাকলে/malformed
  JSON হলে ডিফল্ট রিটার্ন (raise না করে)।

শেষে (Definition of Done):
1. পুরো test suite পাশ করছে কনফার্ম করো (regression সহ):
   `python3 -m unittest discover -s pipeline/tests -v`
2. docs/HANDOFF_NEXT.md: "গ্রুপ A (coverage-gap + duplicate-cluster
   diagnostics, subtitle_qa.json) সম্পূর্ণ, wired, তবে এখনো কোনো
   auto-repair বা UI নেই। পরের কাজ: গ্রুপ B (B1 থেকে — targeted
   re-extraction repair)।"
3. docs/CHANGELOG.md-এ এন্ট্রি যোগ করো।
4. Commit: "chunk A3: wire gap/duplicate-cluster diagnostics into build_subtitle_list() -> subtitle_qa.json" — push করো।
5. Tag: git tag chunk-A3-done && git push origin chunk-A3-done
```

---
---

## গ্রুপ B — Flag হওয়া রেঞ্জের জন্য Targeted Re-extraction Repair

এই গ্রুপ সবচেয়ে ঝুঁকিপূর্ণ (external Gemini API + video segmentation +
timestamp offset math), তাই ৪টা সাব-চাংকে ভাগ করা।

### B1 — Windowed extraction ফাংশন (বিশুদ্ধ নতুন ফাংশন, wiring নেই)

```
আমি manhwa-video-dubber-v6-এর subtitle-QA-fix আপডেটে কাজ করছি, GitHub-ভিত্তিক
চেইনের "চাংক B1" (A3 শেষ, গ্রুপ A সম্পূর্ণ)।

Repo: https://github.com/shafin262619-jpg/manhwa-video-dubber-v6.git  (branch: main)

প্রথমে করো:
1. Repo clone/pull করো, tag chunk-A3-done থেকে ভেরিফাই করো।
2. docs/HANDOFF_NEXT.md পড়ো।
3. pipeline/subtitle_extract.py পুরোটা পড়ো — বিশেষ করে `_run_ffmpeg()`,
   `_segment_video()` (ffmpeg দিয়ে সময়-রেঞ্জ কাটার প্যাটার্ন),
   `_call_gemini()`, `_parse_subtitles()` (offset_sec যোগ করার প্যাটার্ন),
   আর `call_with_rotation()` (key rotation resilience wrapper)।

তোমার স্কোপ:

`pipeline/subtitle_extract.py`-তে একটা নতুন ফাংশন যোগ করো যেটা একটা
নির্দিষ্ট সময়-রেঞ্জ শুধু re-extract করে:

```python
def extract_window(job_id, start_sec, end_sec, upload_root=None,
                    call_budget=None, logger_=None):
    """[start_sec, end_sec) রেঞ্জটা source.mp4 থেকে আলাদা ক্লিপ কেটে
    (_run_ffmpeg, ঠিক _segment_video()-এর মতোই -ss/-to/-c copy প্যাটার্ন
    ব্যবহার করে) সেটাকে আলাদাভাবে Gemini-তে পাঠায় (SUBTITLE_EXTRACT_PROMPT,
    _call_gemini() না, call_with_rotation() দিয়ে সরাসরি — যেন key rotation/
    content-block resilience বজায় থাকে) আর offset_sec=start_sec দিয়ে
    absolute timing-এ subtitle লিস্ট রিটার্ন করে।

    ক্লিপ ফাইল লেখা হবে job_dir / "repair_segments" / এ (নতুন সাবফোল্ডার,
    "segments"-এর সাথে গুলিয়ে ফেলবে না, যেন গ্রুপ A-এর মূল
    subtitles_zh_raw.json/subtitles_zh.json-এর সাথে conflict না হয়)।

    Gemini fail করলে (call_with_rotation None রিটার্ন করলে) বা parse
    ব্যর্থ হলে — কখনো raise করবে না, None রিটার্ন করবে (existing
    resilience pattern-এর মতোই)। সফল হলে subtitle dict-এর লিস্ট রিটার্ন
    করবে (প্রতিটায় absolute start_sec/end_sec/text)।
    """
```

hard constraint:
- ffmpeg কমান্ড ঠিক `_segment_video()`-এর মতো `-c copy` ব্যবহার করবে
  (re-encode না, দ্রুত/lossless কাটার জন্য)।
- এই ফাংশন `build_subtitle_list()` বা app.py-তে এখনই wire করবে না —
  এটা এখন standalone, শুধু সরাসরি কল করলে কাজ করবে। Wiring B2/B3-এর
  কাজ।
- `.gitignore`-এ `repair_segments/` আলাদা করে যোগ করার দরকার নেই — এটা
  `uploads/` এর ভেতরে থাকবে যেটা ইতিমধ্যেই ignored।

টেস্ট (`pipeline/tests/test_subtitle_extract.py`-তে যোগ করো, বিদ্যমান
mocked-Gemini প্যাটার্ন অনুসরণ করে):
- সফল কল: mocked Gemini response, verify করো subtitle-গুলোর
  start_sec/end_sec ঠিকমতো `start_sec` অফসেট পেয়েছে (উইন্ডোর ভেতরের
  relative time না, absolute)।
- Gemini failure (সব key fail) → None রিটার্ন, raise হয় না।
- Malformed JSON response → None রিটার্ন, raise হয় না।
- ffmpeg কল সঠিক args (`-ss`, `-to`, `-c copy`) নিয়ে হয়েছে verify করো
  (mocked subprocess)।

শেষে (Definition of Done):
1. পুরো test suite পাশ করছে কনফার্ম করো:
   `python3 -m unittest discover -s pipeline/tests -v`
2. docs/HANDOFF_NEXT.md: "B1 (extract_window, standalone) সম্পূর্ণ। পরের
   কাজ: B2 (repair orchestration — কোন রেঞ্জে চালাতে হবে সেটা গ্রুপ A-এর
   diagnostics থেকে ঠিক করা, ফলাফল merge করা)।"
3. docs/CHANGELOG.md-এ এন্ট্রি যোগ করো।
4. Commit: "chunk B1: subtitle_extract.extract_window() — targeted time-range re-extraction (standalone)" — push করো।
5. Tag: git tag chunk-B1-done && git push origin chunk-B1-done
```

### B2 — Repair orchestration (diagnostics খেয়ে windowed-repair চালানো + merge)

```
আমি manhwa-video-dubber-v6-এর subtitle-QA-fix আপডেটে কাজ করছি, GitHub-ভিত্তিক
চেইনের "চাংক B2" (B1 শেষ)।

Repo: https://github.com/shafin262619-jpg/manhwa-video-dubber-v6.git  (branch: main)

প্রথমে করো:
1. Repo clone/pull করো, tag chunk-B1-done থেকে ভেরিফাই করো।
2. docs/HANDOFF_NEXT.md পড়ো।
3. pipeline/subtitle_builder.py-র `detect_gaps()`, `detect_duplicate_clusters()`,
   `_serialize()` (গ্রুপ A) আর pipeline/subtitle_extract.py-র
   `extract_window()` (B1) দুটোই ভালোভাবে পড়ো।
4. pipeline/translator.py-র `_repair_split()`-এর bounded-recursion প্যাটার্ন
   দেখো (max_split_rounds দিয়ে infinite loop আটকানো) — এখানে একই স্পিরিট
   ব্যবহার করবে (bounded attempts, pathological input হলে gracefully
   fallback)।

তোমার স্কোপ:

`pipeline/subtitle_builder.py`-তে একটা নতুন ফাংশন:

```python
def repair_flagged_regions(job_id, entries, diagnostics, upload_root=None,
                            call_budget=None, logger_=None,
                            max_attempts=None):
    """diagnostics (A3-এর dict: gaps + duplicate_clusters) থেকে repair
    দরকার এমন সময়-রেঞ্জের একটা লিস্ট বানায়, প্রতিটার জন্য
    subtitle_extract.extract_window() কল করে, সফল হলে সেই রেঞ্জের ভেতরের
    পুরনো raw entries বাদ দিয়ে নতুন এন্ট্রি বসায়, তারপর পুরো লিস্ট
    আবার _serialize() দিয়ে rebuild করে রিটার্ন করে।

    রেঞ্জ-লিস্ট বানানো: প্রতিটা gap -> {gap_start_sec, gap_end_sec};
    প্রতিটা duplicate_cluster -> {cluster-এর প্রথম আর শেষ এন্ট্রির
    start_sec/end_sec থেকে একটা রেঞ্জ, দুই পাশে config.SUBTITLE_OVERLAP_SEC
    এর অর্ধেক padding যোগ করে} যাতে ঠিক boundary-তে থাকা কনটেন্টও ধরা পড়ে।
    Overlapping রেঞ্জ থাকলে merge করে একটাতে নাও (redundant Gemini কল
    এড়াতে)।

    max_attempts None হলে config.SUBTITLE_MAX_REPAIR_ATTEMPTS ব্যবহার করো
    (এই চাংকেই config.py-তে যোগ করো, ডিফল্ট 3)। রেঞ্জ-লিস্ট বড় হলে
    সবচেয়ে বড় gap_sec/count-এর রেঞ্জগুলো আগে (সবচেয়ে গুরুত্বপূর্ণ প্রথমে),
    max_attempts-এর বেশি রেঞ্জ চালানো হবে না — বাকিগুলো "skipped_budget"
    হিসেবে রিটার্নের মধ্যে থেকে যাবে (repair হবে না, কিন্তু raise/crash
    হবে না)।

    রিটার্ন করে (repaired_entries, repair_summary) যেখানে repair_summary:
        {"attempted": N, "succeeded": M, "failed": N-M,
         "skipped_budget": [...রেঞ্জ যেগুলো max_attempts-এর কারণে বাদ...]}

    extract_window() None রিটার্ন করলে (Gemini fail) সেই রেঞ্জ স্কিপ করে
    পরেরটায় যায় — কখনো raise করে না, একটা রেঞ্জ fail করলেও বাকিগুলো
    চলতে থাকে।
    """
```

hard constraint:
- `call_budget` প্যারামিটার সরাসরি `extract_window()`-এ পাস করবে, যাতে
  repair কলগুলো app.py-র শেয়ার্ড per-job `gemini_rotation.CallBudget`-এর
  ভেতরেই গোনা হয় (গ্রুপ B3-এ wire করা হবে) — repair mechanism যেন কখনো
  budget-এর বাইরে গিয়ে unlimited কল না করে।
- rebuild করার সময় বাদ দেওয়া পুরনো এন্ট্রি + নতুন যোগ হওয়া এন্ট্রি —
  দুটোই `_serialize()`-এ যাওয়ার আগে ঠিক আগের মতো raw entry dict ফরম্যাটে
  থাকতে হবে (`{"text_zh", "status", "start_sec", "end_sec"}`), যাতে
  serial নম্বর নতুন করে সঠিকভাবে বসে।
- এই চাংকে এখনো `build_subtitle_list()` বা app.py স্পর্শ করবে না — শুধু
  বিশুদ্ধ orchestration ফাংশন এই একটা জায়গায়, wire করা B3-এর কাজ।

`config.py`-তে যোগ করো:
```python
# Max number of targeted re-extraction (Gemini) calls a single job's repair
# pass may make, largest-flagged-range-first (QA repair, B2). Protects the
# shared per-job CallBudget from a runaway repair loop.
SUBTITLE_MAX_REPAIR_ATTEMPTS = 3
```

টেস্ট (`pipeline/tests/test_subtitle_builder.py`):
- mocked `extract_window()` দিয়ে: একটা gap সফলভাবে repair হয় → নতুন
  এন্ট্রি ঠিক জায়গায় বসে, `repair_summary["succeeded"] == 1`।
- একটা duplicate cluster সফলভাবে repair হয় → পুরনো cluster এন্ট্রি বাদ
  পড়ে, নতুন এন্ট্রি বসে।
- `extract_window()` None রিটার্ন করলে (fail) সেই রেঞ্জ untouched থাকে,
  `repair_summary["failed"] == 1`, কোনো raise হয় না।
- flag-সংখ্যা `max_attempts`-এর বেশি হলে সবচেয়ে বড়গুলো আগে চলে, বাকিগুলো
  `skipped_budget`-এ থাকে।
- Overlapping রেঞ্জ merge হয় (extract_window() ঠিক যতবার কল হওয়া উচিত
  ততবারই কল হয়েছে, `assert_called_...` দিয়ে ভেরিফাই)।
- কোনো diagnostics flag না থাকলে (gaps=[], duplicate_clusters=[]) →
  entries অপরিবর্তিত, `extract_window()` মোটেও কল হয় না।

শেষে (Definition of Done):
1. পুরো test suite পাশ করছে কনফার্ম করো:
   `python3 -m unittest discover -s pipeline/tests -v`
2. docs/HANDOFF_NEXT.md: "B2 (repair_flagged_regions, standalone
   orchestration) সম্পূর্ণ। পরের কাজ: B3 (build_subtitle_list() +
   app.py-তে wire করা — end-to-end auto-repair)।"
3. docs/CHANGELOG.md-এ এন্ট্রি যোগ করো।
4. Commit: "chunk B2: subtitle_builder.repair_flagged_regions() — bounded targeted-repair orchestration" — push করো।
5. Tag: git tag chunk-B2-done && git push origin chunk-B2-done
```

### B3 — `build_subtitle_list()` + `app.py`-তে wiring (end-to-end auto-repair)

```
আমি manhwa-video-dubber-v6-এর subtitle-QA-fix আপডেটে কাজ করছি, GitHub-ভিত্তিক
চেইনের "চাংক B3" (B2 শেষ)।

Repo: https://github.com/shafin262619-jpg/manhwa-video-dubber-v6.git  (branch: main)

প্রথমে করো:
1. Repo clone/pull করো, tag chunk-B2-done থেকে ভেরিফাই করো।
2. docs/HANDOFF_NEXT.md পড়ো।
3. app.py-র `_run_upload_pipeline()` পড়ো — বিশেষ করে এই অংশ:
   ```python
   budget = gemini_rotation.CallBudget(config.MAX_API_CALLS_PER_JOB)
   extraction = subtitle_extract.extract_subtitles(job_id, call_budget=budget)
   subtitle_builder.build_subtitle_list(job_id)
   translation = translator.translate_subtitles(job_id, call_budget=budget)
   ```
   — এই একটাই জায়গায় auto-repair বসাতে হবে।

তোমার স্কোপ:

**১.** `build_subtitle_list()`-এর সিগনেচার বাড়াও (backward-compat রেখে):
```python
def build_subtitle_list(job_id, upload_root=None, call_budget=None, auto_repair=True):
```
`call_budget=None` আর `auto_repair=True` ডিফল্ট রাখো যাতে বিদ্যমান কল-সাইট
(পুরনো টেস্ট সহ, যারা `build_subtitle_list(job_id)` বা
`build_subtitle_list(job_id, upload_root)` কল করে) আগের মতোই কাজ করে।

ভেতরে: A3-এর diagnostics বসানোর পরে, `auto_repair=True` আর
(`gaps` বা `duplicate_clusters` নন-এম্পটি) হলে —
`repair_flagged_regions()` (B2) কল করো, ফলাফল দিয়ে entries rebuild করো,
**তারপর diagnostics আবার নতুন করে চালাও** (repair-এর পরের অবস্থার ওপর
`detect_gaps`/`detect_duplicate_clusters`, যাতে এখনো কী বাকি আছে সেটা
জানা যায়), আর `subtitle_qa.json`-এ এই আপডেটেড diagnostics + একটা
`"repair"` কী (B2-এর `repair_summary`) যোগ করো। ফাইনাল serialized
লিস্টটাই (repair-পরবর্তী) `subtitles_zh.json`-এ লেখা হবে আর ফাংশনের
রিটার্ন ভ্যালু হবে — **রিটার্ন-টাইপ এখনো শুধু লিস্ট**, dict না।

**২.** `app.py`-র `_run_upload_pipeline()`-এ কলটা বদলাও:
```python
subtitle_builder.build_subtitle_list(job_id, call_budget=budget)
```
(শেয়ার্ড per-job budget-টাই পাস করবে, যাতে extraction + repair +
translation সব মিলিয়ে একটাই `config.MAX_API_CALLS_PER_JOB` cap মানে —
এটাই আগের ডিজাইন-নিয়ম, U2b-তে যেভাবে extraction+translation শেয়ার করত
সেটা এখন repair-ও শেয়ার করবে।)

hard constraint:
- Repair fail করলে (Gemini/budget exhausted) — কখনো `_run_upload_pipeline()`
  crash করবে না, pipeline আগের (un-repaired কিন্তু flagged) subtitle
  লিস্ট নিয়েই এগিয়ে যাবে (existing resilience-এর ধারাবাহিকতা)।
- `_resume_pipeline_extra()` (idempotent resume path, যখন
  `subtitles_hi.json` আগে থেকেই আছে) স্পর্শ করার দরকার নেই — repair শুধু
  fresh run-এ হবে।

টেস্ট:
- `pipeline/tests/test_subtitle_builder.py`: mocked `extract_window`
  দিয়ে `build_subtitle_list(auto_repair=True)` পুরো flow টেস্ট করো (flag
  → repair → re-diagnose → subtitle_qa.json-এ repair summary + updated
  gaps/clusters)। `auto_repair=False` দিলে repair স্কিপ হয় (raw diagnostics
  অপরিবর্তিত থাকে) সেটাও টেস্ট করো।
- `pipeline/tests/test_app_orchestration.py`-তে বিদ্যমান upload-pipeline
  টেস্টগুলো (mocked Gemini) অপরিবর্তিত পাশ করে কনফার্ম করো (regression) —
  repair mocked response খালি/কোনো ফ্ল্যাগ না থাকলে repair কল হবেই না,
  তাই এই টেস্টগুলোর mock বদলানোর দরকার হওয়া উচিত না।
- একটা নতুন integration-স্টাইল টেস্ট যোগ করো (mocked Gemini, fixture
  raw data যাতে ইচ্ছাকৃতভাবে একটা duplicate-cluster থাকে) যেটা পুরো
  `subtitle_extract.extract_subtitles → subtitle_builder.build_subtitle_list`
  চেইন চালিয়ে repair আসলেই কাজ করছে ভেরিফাই করে।

শেষে (Definition of Done):
1. পুরো test suite পাশ করছে কনফার্ম করো:
   `python3 -m unittest discover -s pipeline/tests -v`
2. `python3 -m py_compile app.py pipeline/subtitle_builder.py`
3. docs/HANDOFF_NEXT.md: "B3 (end-to-end auto-repair wired into
   upload_pipeline) সম্পূর্ণ। গ্রুপ B বাকি: B4 (পূর্ণাঙ্গ টেস্ট স্যুট +
   edge cases: budget-exhausted, whole-repair-failed)।"
4. docs/CHANGELOG.md-এ এন্ট্রি যোগ করো।
5. Commit: "chunk B3: wire auto-repair into build_subtitle_list() + app.py upload_pipeline (shared CallBudget)" — push করো।
6. Tag: git tag chunk-B3-done && git push origin chunk-B3-done
```

### B4 — গ্রুপ B-এর সম্পূর্ণ টেস্ট স্যুট + edge cases

```
আমি manhwa-video-dubber-v6-এর subtitle-QA-fix আপডেটে কাজ করছি, GitHub-ভিত্তিক
চেইনের "চাংক B4" (B3 শেষ, গ্রুপ B-এর শেষ সাব-চাংক)। এই চাংকের কাজ মূলত
verification + edge-case coverage — কোনো বড় নতুন ফিচার না।

Repo: https://github.com/shafin262619-jpg/manhwa-video-dubber-v6.git  (branch: main)

প্রথমে করো:
1. Repo clone/pull করো, tag chunk-B3-done থেকে ভেরিফাই করো।
2. docs/HANDOFF_NEXT.md পড়ো।
3. `pipeline/gemini_rotation.py`-র `CallBudget`/`CallBudgetExceeded` পড়ো
   (repair-কলগুলো budget-exhausted অবস্থায় কীভাবে ব্যর্থ হয় বোঝার জন্য)।

তোমার স্কোপ:

গ্রুপ B (B1-B3) জুড়ে এই edge-case গুলো টেস্টে coverage আছে কিনা যাচাই
করো, না থাকলে যোগ করো:

1. **Budget exhausted মাঝ-repair-এ**: `CallBudget` খুব ছোট (যেমন ১) সেট
   করে, একাধিক flag থাকা fixture দিয়ে — প্রথম repair কলের পরেই budget
   ফুরিয়ে গেলে বাকি রেঞ্জগুলো gracefully স্কিপ হয় (raise না করে),
   `repair_summary`-তে সেটা প্রতিফলিত হয়।
2. **সব repair কল ব্যর্থ**: প্রতিটা `extract_window()` কল None রিটার্ন
   করলে — পুরনো (un-repaired) diagnostics-ই `subtitle_qa.json`-এ থেকে
   যায়, pipeline crash করে না, `subtitles_zh.json` আগের (un-repaired)
   এন্ট্রি নিয়েই লেখা হয়।
3. **Repair নিজেই নতুন duplicate তৈরি করলে**: নতুন Gemini রেসপন্সে যদি
   আবার duplicate timestamp থাকে — সেটা re-diagnose ধাপে ধরা পড়বে
   (subtitle_qa.json-এর updated gaps/duplicate_clusters-এ দেখাবে), কিন্তু
   B3 অনুযায়ী **আবার repair চালানো হবে না** (single-pass repair —
   একবারই চালানো হয়, recursive repair না) — এটা স্পষ্টভাবে টেস্টে
   ভেরিফাই করো আর docs/HANDOFF_NEXT.md-এ known-limitation হিসেবে নোট
   করো (ভবিষ্যতে recursive repair চাইলে সেটা একটা নতুন চাংক হবে)।
4. **Idempotent resume path**: `subtitles_hi.json` আগে থেকে থাকলে
   `_run_upload_pipeline()` `build_subtitle_list()`-ই কল করে না (B3-এ
   অপরিবর্তিত রাখা হয়েছিল) — এই assumption এখনো সত্যি কনফার্ম করো।
5. পুরো `pipeline/tests/test_subtitle_extract.py` +
   `pipeline/tests/test_subtitle_builder.py` +
   `pipeline/tests/test_app_orchestration.py` +
   `pipeline/tests/test_full_auto_orchestration.py` — সব একসাথে চালিয়ে
   কোনো flakiness/regression নেই কনফার্ম করো।

শেষে (Definition of Done):
1. পুরো test suite ১০০% পাশ করছে কনফার্ম করো (চূড়ান্ত টেস্ট কাউন্ট লিখে
   রাখো):
   `python3 -m unittest discover -s pipeline/tests -v`
2. docs/HANDOFF_NEXT.md: "গ্রুপ B (targeted re-extraction repair, single-pass,
   bounded by SUBTITLE_MAX_REPAIR_ATTEMPTS + shared CallBudget) সম্পূর্ণ,
   edge-case টেস্টেড। পরের কাজ: গ্রুপ C (C1 থেকে — sub-chunking থ্রেশহোল্ড
   কমানো)।"
3. docs/CHANGELOG.md-এ এন্ট্রি যোগ করো (চূড়ান্ত টেস্ট কাউন্ট সহ)।
4. Commit: "chunk B4: repair edge-case coverage (budget exhaustion, total failure, single-pass limitation)" — push করো।
5. Tag: git tag chunk-B4-done && git push origin chunk-B4-done
```

---
---

## গ্রুপ C — ছোট/Dialogue-dense ভিডিওতেও Sub-chunking থ্রেশহোল্ড কমানো

`config.LONG_VIDEO_CHUNK_THRESHOLD_SEC` দুটো কাজ একসাথে করে:
(ক) ভিডিও chunk হবে কিনা তার থ্রেশহোল্ড, (খ) chunk হলে প্রতিটা সেগমেন্ট
কত সেকেন্ড হবে (`_segment_video()`/`_segment_ranges()` দুই জায়গাতেই একই
constant ব্যবহার করে)। তাই এই একটা constant কমালেই দুটো effect একসাথে
আসে — এই মুহূর্তে `600`, অথচ আপনার আপলোড করা ~৫-৬ মিনিটের ভিডিও এর নিচেই
পড়ে যায় বলে মোটেও chunk হয়নি।

### C1 — Config default কমানো + inline ডকুমেন্টেশন

```
আমি manhwa-video-dubber-v6-এর subtitle-QA-fix আপডেটে কাজ করছি, GitHub-ভিত্তিক
চেইনের "চাংক C1" (B4 শেষ, গ্রুপ B সম্পূর্ণ)।

Repo: https://github.com/shafin262619-jpg/manhwa-video-dubber-v6.git  (branch: main)

প্রথমে করো:
1. Repo clone/pull করো, tag chunk-B4-done থেকে ভেরিফাই করো।
2. docs/HANDOFF_NEXT.md পড়ো।
3. pipeline/config.py-তে `LONG_VIDEO_CHUNK_THRESHOLD_SEC`,
   `SUBTITLE_OVERLAP_SEC` পড়ো (কমেন্ট সহ)। pipeline/subtitle_extract.py-র
   `_segment_video()` (লাইন ~157-158) আর
   `extract_subtitles()`-এর chunked-decision (লাইন ~414) দুই জায়গায়
   কীভাবে এই constant ব্যবহার হয় দেখো।
4. `grep -rn LONG_VIDEO_CHUNK_THRESHOLD_SEC pipeline/` চালিয়ে সব
   ব্যবহার-জায়গা (module কোড + টেস্ট, দুটোই) লিস্ট করো — এটাই একমাত্র
   জায়গা যেখানে এই constant পড়া হয়, তাই শুধু ভ্যালু বদলালেই যথেষ্ট,
   কোনো লজিক বদলাতে হবে না।

তোমার স্কোপ:

`pipeline/config.py`-তে `LONG_VIDEO_CHUNK_THRESHOLD_SEC`-এর ভ্যালু `600`
থেকে `90`-এ নামাও, আর কমেন্ট আপডেট করো একটা "DELIBERATE" নোট দিয়ে (ঠিক
`TTS_MODEL`-এর কমেন্টের স্টাইলে) — কেন কমানো হলো সেটা ব্যাখ্যা করে:

```python
# Videos longer than this (seconds) are chunked as B1 before processing.
#
# DELIBERATE (chunk C1, corrected after a real-world failure): originally
# 600s. A real ~5-6 minute dialogue-dense manhwa-dub video came in UNDER
# that threshold and was sent to Gemini in a single call — the model
# dropped a ~50-second/37-line dialogue-heavy block entirely and mis-timed
# several others into duplicate-timestamp clusters. Lowering this to 90s
# means even short videos get segmented into smaller, easier-for-Gemini
# chunks (with SUBTITLE_OVERLAP_SEC overlap + dedup, same as before), which
# both improves per-segment timestamp accuracy and reduces missed dialogue.
# The trade-off is more Gemini calls per job (still capped by
# MAX_API_CALLS_PER_JOB) and more ffmpeg segment-cutting time.
LONG_VIDEO_CHUNK_THRESHOLD_SEC = 90
```

hard constraint: শুধু এই একটা ভ্যালু বদলাও, `_segment_video()`,
`_segment_ranges()`, বা chunked-decision লজিকের কোনো কোড বদলাবে না —
বিদ্যমান লজিক এই constant-নির্ভর, ভ্যালু কমালেই কাঙ্ক্ষিত effect আসে।

বিদ্যমান টেস্ট (`test_subtitle_builder.py`, `test_subtitle_extract.py`)
`LONG_VIDEO_CHUNK_THRESHOLD_SEC`-কে `mock.patch` দিয়ে `2.0`-এ ওভাররাইড
করে চালায় — এগুলো এই ডিফল্ট-বদলে প্রভাবিত হওয়া উচিত না, কিন্তু চালিয়ে
কনফার্ম করো।

শেষে (Definition of Done):
1. পুরো test suite পাশ করছে কনফার্ম করো:
   `python3 -m unittest discover -s pipeline/tests -v`
2. docs/HANDOFF_NEXT.md: "C1 (LONG_VIDEO_CHUNK_THRESHOLD_SEC 600→90)
   সম্পূর্ণ। পরের কাজ: C2 (এই বদলের ফলে যেসব real-fixture টেস্ট এখন
   ffmpeg segment-cutting পাথে গিয়ে পড়ছে সেগুলোর regression চেক)।"
3. docs/CHANGELOG.md-এ এন্ট্রি যোগ করো (এই config বদলের কারণ স্পষ্ট করে
   লিখে, যেমন TTS_MODEL এন্ট্রির স্টাইলে)।
4. Commit: "chunk C1: lower LONG_VIDEO_CHUNK_THRESHOLD_SEC 600s -> 90s (always sub-chunk dialogue-dense short videos)" — push করো।
5. Tag: git tag chunk-C1-done && git push origin chunk-C1-done
```

### C2 — এই থ্রেশহোল্ড-বদলের Regression চেক (real-fixture ভিডিও পাথ)

```
আমি manhwa-video-dubber-v6-এর subtitle-QA-fix আপডেটে কাজ করছি, GitHub-ভিত্তিক
চেইনের "চাংক C2" (C1 শেষ, গ্রুপ C-এর শেষ সাব-চাংক)। এই চাংকের কাজ
verification + প্রয়োজনে ছোট ফিক্স — কোনো নতুন ফিচার না।

Repo: https://github.com/shafin262619-jpg/manhwa-video-dubber-v6.git  (branch: main)

প্রথমে করো:
1. Repo clone/pull করো, tag chunk-C1-done থেকে ভেরিফাই করো।
2. docs/HANDOFF_NEXT.md পড়ো।
3. `pipeline/tests/test_video_ingest.py`, `test_subtitle_extract.py`,
   `test_app_orchestration.py`, `test_full_auto_orchestration.py`-তে
   real ffmpeg দিয়ে বানানো fixture ভিডিওর দৈর্ঘ্য কত সেকেন্ড খুঁজে বের
   করো (`grep -n "duration\|_make_.*video\|generate.*video"`)।

তোমার স্কোপ:

C1-এ `LONG_VIDEO_CHUNK_THRESHOLD_SEC` 600 থেকে 90-এ নামানোর ফলে, যেসব
টেস্ট real ffmpeg দিয়ে fixture ভিডিও বানায় কিন্তু `LONG_VIDEO_CHUNK_THRESHOLD_SEC`
mock করে না — যদি তাদের fixture ভিডিও ৯০ সেকেন্ডের বেশি লম্বা হয় (বেশিরভাগ
সম্ভবত অনেক ছোট, কয়েক সেকেন্ডের সিন্থেটিক ক্লিপ, কিন্তু নিশ্চিত করে দেখতে
হবে), তাহলে এখন হঠাৎ chunked পাথে (real ffmpeg segment-cutting + একাধিক
mocked/real Gemini কল) গিয়ে পড়তে পারে যেখানে আগে single-call পাথে যেত।

1. পুরো টেস্ট স্যুট চালাও, কোনো টেস্ট নতুন করে fail/timeout করছে কিনা
   দেখো।
2. Fail করলে root cause বের করো — বেশিরভাগ ক্ষেত্রেই fixture ভিডিও এত
   ছোট হবে যে সমস্যা হবে না; যদি কোনো fixture সত্যিই ৯০+ সেকেন্ডের হয়
   আর টেস্টটা chunking mock করেনি, সেই টেস্টে explicit
   `mock.patch("pipeline.config.LONG_VIDEO_CHUNK_THRESHOLD_SEC", <বড় মান>)`
   যোগ করো (বিদ্যমান প্যাটার্ন অনুসরণ করে) যাতে সেই নির্দিষ্ট টেস্টের
   উদ্দেশ্য (যেটা chunking টেস্ট করা না) অক্ষুণ্ণ থাকে — কোনো প্রোডাকশন
   কোড বদলাবে না, শুধু টেস্ট-লেভেল fixture/mock ঠিক করো।
3. একটা নতুন ছোট টেস্ট যোগ করো যেটা স্পষ্টভাবে ভেরিফাই করে: একটা ~২-৩
   মিনিটের (৯০ সেকেন্ডের বেশি, ৬০০ সেকেন্ডের অনেক নিচে) ভিডিও এখন
   `chunked=True` হিসেবে extract হয় (আগে যেটা `chunked=False` হতো) —
   mocked Gemini দিয়ে, `job_meta.json`-এ `duration_sec` সরাসরি বসিয়ে
   (real ffmpeg fixture বানানোর দরকার নেই, `_load_job_meta` duration
   সরাসরি পড়ে)।

hard constraint: এই চাংকে কোনো প্রোডাকশন লজিক বদলাবে না (C1-এই সেটা হয়ে
গেছে) — শুধু টেস্ট-লেভেল ভেরিফিকেশন + প্রয়োজনে টেস্ট fixture/mock ফিক্স।

শেষে (Definition of Done):
1. পুরো test suite ১০০% পাশ করছে কনফার্ম করো:
   `python3 -m unittest discover -s pipeline/tests -v`
2. docs/HANDOFF_NEXT.md: "গ্রুপ C (৯০-সেকেন্ড sub-chunking threshold,
   regression-verified) সম্পূর্ণ। পরের কাজ: গ্রুপ D (D1 থেকে — independent
   local-Whisper cross-check)।"
3. docs/CHANGELOG.md-এ এন্ট্রি যোগ করো।
4. Commit: "chunk C2: regression-verify 90s chunking threshold against real-fixture tests + add explicit chunked=True coverage test" — push করো।
5. Tag: git tag chunk-C2-done && git push origin chunk-C2-done
```

---
---

## গ্রুপ D — Independent Local-Whisper Cross-check Verification Pass

`pipeline/voiceover_upload.py`-তে ইতিমধ্যেই "Gemini fail → local Whisper
fallback" রেজিলিয়েন্স প্যাটার্ন আছে (`_transcribe_words()`,
`config.WHISPER_MODEL`, lazy `import whisper`)। গ্রুপ D একই স্টাইল দিয়ে
extraction-এর (B1) একটা independent rough cross-check বানাবে — Gemini-র
আউটপুটকে replace না করে, শুধু "এটা কতটা বিশ্বাসযোগ্য" flag করার জন্য।

### D1 — `pipeline/subtitle_verify.py` (নতুন মডিউল, standalone)

```
আমি manhwa-video-dubber-v6-এর subtitle-QA-fix আপডেটে কাজ করছি, GitHub-ভিত্তিক
চেইনের "চাংক D1" (C2 শেষ, গ্রুপ C সম্পূর্ণ)।

Repo: https://github.com/shafin262619-jpg/manhwa-video-dubber-v6.git  (branch: main)

প্রথমে করো:
1. Repo clone/pull করো, tag chunk-C2-done থেকে ভেরিফাই করো।
2. docs/HANDOFF_NEXT.md পড়ো।
3. pipeline/voiceover_upload.py-র `_transcribe_words()` (lazy whisper
   import, error-handling প্যাটার্ন — `try/except ImportError`,
   `try/except Exception` উভয়ই resilience-এর জন্য) আর
   `pipeline/voiceover_auto.py`-র `_probe_audio_duration()`, `_run()`
   (ffmpeg subprocess wrapper) পড়ো।

তোমার স্কোপ:

নতুন ফাইল `pipeline/subtitle_verify.py` বানাও:

```python
"""Independent local-Whisper cross-check for Chinese subtitle extraction (D1).

Extraction (B1, subtitle_extract.py) relies entirely on Gemini video
understanding. This module runs a lightweight, local, audio-only
double-check using Whisper so a large Gemini extraction failure (missing
dialogue block, hallucinated content) can be flagged even without a human
manually reading the SRT. Never treated as ground truth, never replaces
Gemini's output -- purely a coverage/sanity signal.
"""

def whisper_cross_check(job_id, upload_root=None, logger_=None):
    """source.mp4 থেকে অডিও এক্সট্র্যাক্ট করে (ffmpeg, mono wav,
    config.TTS_SAMPLE_RATE, ঠিক voiceover_upload._convert_to_wav()-এর
    প্যাটার্নে — এই ফাইলেই ছোট করে পুনরায় লেখো, cross-import এড়াতে বা
    voiceover_upload থেকে import করো, যেটা কম duplicate কোড দেয় সেটা
    বেছে নাও), config.WHISPER_MODEL দিয়ে transcribe করে (word_timestamps
    দরকার নেই এখানে, শুধু segment-level, ভাষা auto-detect — চাইনিজ অডিও)।

    রিটার্ন করে:
        {
            "status": "ok" | "skipped" | "mismatch",
            "reason": None | "whisper_not_installed" | "transcription_failed",
            "whisper_spoken_sec": <সব whisper segment-এর মোট duration>,
            "extracted_covered_sec": <subtitle_qa.json-এর covered_duration_sec,
                                        A3 থেকে load_subtitle_qa() দিয়ে>,
            "coverage_ratio": extracted_covered_sec / whisper_spoken_sec
                               (whisper_spoken_sec শূন্য হলে None),
            "mismatch": bool,   # ratio config.SUBTITLE_COVERAGE_MISMATCH_RATIO
                                 এর নিচে হলে True
        }

    Whisper ইনস্টল না থাকলে বা transcription fail করলে (import/runtime
    error) — raise না করে {"status": "skipped", "reason": ...} রিটার্ন
    করে। কখনো raise করে না।

    ফলাফল uploads/<job_id>/subtitle_qa_whisper.json এ লিখে রাখে।
    """
```

hard constraint:
- Lazy `import whisper` (module-টপ-লেভেলে না, ফাংশনের ভেতরে) —
  `_transcribe_words()`-এর মতোই, যাতে whisper ইনস্টল না থাকা environment
  -এ (যেমন CI) মডিউল ইম্পোর্টই fail না করে।
- ffmpeg/whisper না থাকলে বা কোনো কারণে fail করলে **কখনো raise করবে
  না** — সবসময় একটা dict রিটার্ন করবে।
- এই চাংকে `build_subtitle_list()`, `extract_subtitles()`, বা app.py
  কিছুই স্পর্শ করবে না — সম্পূর্ণ standalone নতুন মডিউল, wiring D2-এর
  কাজ।

`config.py`-তে যোগ করো:
```python
# whisper_cross_check() flags a mismatch when Gemini-extracted covered
# duration is below this fraction of Whisper's independently-measured
# spoken-audio duration (QA verification, D1).
SUBTITLE_COVERAGE_MISMATCH_RATIO = 0.75
```

টেস্ট (নতুন `pipeline/tests/test_subtitle_verify.py`, বিদ্যমান
`test_voiceover_upload.py`-র mocked-whisper প্যাটার্ন অনুসরণ করে —
`sys.modules["whisper"]` mock/patch করার স্টাইল দেখো):
- Whisper ইনস্টল নেই (`ImportError` সিমুলেট) → `status: "skipped"`,
  `reason: "whisper_not_installed"`, raise হয় না।
- Whisper transcription fail করে (mocked exception) → `status: "skipped"`,
  raise হয় না।
- সফল transcription, coverage ratio থ্রেশহোল্ডের ওপরে → `status: "ok"`,
  `mismatch: False`।
- সফল transcription, coverage ratio থ্রেশহোল্ডের নিচে (whisper অনেক বেশি
  স্পিচ ধরেছে কিন্তু extraction অনেক কম covered_duration_sec দেখাচ্ছে)
  → `status: "mismatch"`, `mismatch: True`।
- `subtitle_qa_whisper.json` সঠিক কনটেন্ট নিয়ে লেখা হয়।

শেষে (Definition of Done):
1. পুরো test suite পাশ করছে কনফার্ম করো:
   `python3 -m unittest discover -s pipeline/tests -v`
2. docs/HANDOFF_NEXT.md: "D1 (subtitle_verify.whisper_cross_check,
   standalone) সম্পূর্ণ। পরের কাজ: D2 (upload_pipeline-এ wire করা, non-blocking
   best-effort)।"
3. docs/CHANGELOG.md-এ এন্ট্রি যোগ করো।
4. Commit: "chunk D1: pipeline/subtitle_verify.py — standalone local-Whisper coverage cross-check" — push করো।
5. Tag: git tag chunk-D1-done && git push origin chunk-D1-done
```

### D2 — `app.py` upload-pipeline-এ wiring (best-effort, non-blocking)

```
আমি manhwa-video-dubber-v6-এর subtitle-QA-fix আপডেটে কাজ করছি, GitHub-ভিত্তিক
চেইনের "চাংক D2" (D1 শেষ)।

Repo: https://github.com/shafin262619-jpg/manhwa-video-dubber-v6.git  (branch: main)

প্রথমে করো:
1. Repo clone/pull করো, tag chunk-D1-done থেকে ভেরিফাই করো।
2. docs/HANDOFF_NEXT.md পড়ো।
3. app.py-র `_run_upload_pipeline()` (B3-এ বদলানো ভার্সন) আবার পড়ো।

তোমার স্কোপ:

`app.py`-র `_run_upload_pipeline()`-এ, `subtitle_builder.build_subtitle_list(...)`
কলের ঠিক পরে (translation শুরুর আগে বা পরে — Whisper cross-check
translation-এর ওপর নির্ভর করে না, তাই extraction-এর ঠিক পরে, translation
শুরুর *আগে* বসালে ইউজার আগে থেকেই signal পাবে, কিন্তু blocking না — এই
অর্ডারেই বসাও):

```python
try:
    subtitle_verify.whisper_cross_check(job_id, logger_=job_logger)
except Exception as exc:  # noqa: BLE001 — best-effort, never break upload_pipeline
    logger.warning("whisper cross-check failed for job %s (non-fatal): %s", job_id, exc)
```

hard constraint: `whisper_cross_check()` নিজেই raise করে না (D1-এর
কন্ট্রাক্ট), কিন্তু তবুও একটা defensive try/except রাখো — pipeline-এর কোনো
অংশ যেন কখনোই এই best-effort ধাপের কারণে ভেঙে না পড়ে (অন্য সব stage যেমন
`_run_upload_pipeline`-এর বাইরের try/except-এ `FileNotFoundError,
ValueError, RuntimeError`-ই ধরে, whisper/librosa-related exception এর
বাইরেও হতে পারে, তাই bare `Exception` এখানে ঠিক আছে যেহেতু pipeline-ব্রেকিং
না হওয়াটাই মূল constraint)।

`job_status_store.write_status(job_id, "upload_pipeline", "done", extra=extra)`-এর
`extra` dict-এ একটা নতুন কী যোগ করো: `extra["whisper_check_status"]` =
`whisper_cross_check()`-এর রিটার্ন dict-এর `"status"` ফিল্ড (বা
exception হলে `"skipped"`)।

টেস্ট:
- `pipeline/tests/test_app_orchestration.py`: mocked `whisper_cross_check`
  সফল হলে status extra-তে `whisper_check_status` দেখা যায়।
- `whisper_cross_check` raise করলে (mocked side_effect=Exception) —
  পুরো `upload_pipeline` তবুও `"done"` status-এ পৌঁছায় (crash করে না),
  শুধু log warning হয়।
- বিদ্যমান upload-pipeline টেস্টগুলো (auto_tts/user_upload end-to-end
  সহ, `test_full_auto_orchestration.py`) অপরিবর্তিত পাশ করে (regression)
  — এগুলোতে whisper mock না থাকলে whisper আসলে ইনস্টল করা না থাকলে
  বাস্তবেই `status: "skipped"` রিটার্ন হবে, তাই কোনো crash হওয়ার কথা না;
  চালিয়ে কনফার্ম করো।

শেষে (Definition of Done):
1. পুরো test suite পাশ করছে কনফার্ম করো:
   `python3 -m unittest discover -s pipeline/tests -v`
2. `python3 -m py_compile app.py`
3. docs/HANDOFF_NEXT.md: "D2 (whisper cross-check wired into
   upload_pipeline, best-effort/non-blocking) সম্পূর্ণ। পরের কাজ: D3
   (edge-case টেস্ট + regression পাস)।"
4. docs/CHANGELOG.md-এ এন্ট্রি যোগ করো।
5. Commit: "chunk D2: wire whisper_cross_check into app.py upload_pipeline (non-blocking, status-tracked)" — push করো।
6. Tag: git tag chunk-D2-done && git push origin chunk-D2-done
```

### D3 — গ্রুপ D-এর Edge-case টেস্ট + Regression পাস

```
আমি manhwa-video-dubber-v6-এর subtitle-QA-fix আপডেটে কাজ করছি, GitHub-ভিত্তিক
চেইনের "চাংক D3" (D2 শেষ, গ্রুপ D-এর শেষ সাব-চাংক)। মূলত verification —
কোনো বড় নতুন ফিচার না।

Repo: https://github.com/shafin262619-jpg/manhwa-video-dubber-v6.git  (branch: main)

প্রথমে করো:
1. Repo clone/pull করো, tag chunk-D2-done থেকে ভেরিফাই করো।
2. docs/HANDOFF_NEXT.md পড়ো।

তোমার স্কোপ:

1. **Real-environment smoke test** (network/pip থাকলে): `pip install
   openai-whisper --break-system-packages`-এর মতো কিছু চেষ্টা করো (heavy
   dependency, timeout/ব্যর্থ হলে স্কিপ করো এবং স্পষ্ট করে লিখে রাখো) —
   ইনস্টল সম্ভব হলে একটা ছোট synthetic ভিডিও/অডিওতে বাস্তবেই
   `whisper_cross_check()` চালিয়ে raise না করা আর reasonable output
   কনফার্ম করো। সম্ভব না হলে mocked টেস্টগুলোই যথেষ্ট ধরে নাও, কারণটা
   docs/HANDOFF_NEXT.md-এ লেখো।
2. **সম্পূর্ণ রিগ্রেশন পাস**: গ্রুপ A, B, C, D — সব একসাথে মিলিয়ে পুরো
   test suite চালাও, flakiness/order-dependency নেই কনফার্ম করো (একাধিকবার
   চালিয়ে দেখো)।
3. `python3 -m py_compile` দিয়ে touched হওয়া সব ফাইল যাচাই করো (app.py,
   pipeline/subtitle_builder.py, pipeline/subtitle_extract.py,
   pipeline/subtitle_verify.py, pipeline/config.py)।
4. চূড়ান্ত টেস্ট কাউন্ট ডকুমেন্ট করো।

শেষে (Definition of Done):
1. পুরো test suite ১০০% পাশ করছে (বা কোনটা পাশ করছে না আর কেন — যেমন
   heavy whisper dependency install সম্ভব না হলে — স্পষ্ট লেখো)।
2. docs/HANDOFF_NEXT.md: "গ্রুপ D (independent local-Whisper cross-check,
   non-blocking) সম্পূর্ণ, রিগ্রেশন-ভেরিফায়েড। পরের কাজ: গ্রুপ E (E1
   থেকে — সব diagnostics একসাথে করে user-facing QA summary + app.py-তে
   দেখানো)।"
3. docs/CHANGELOG.md-এ এন্ট্রি যোগ করো (চূড়ান্ত টেস্ট কাউন্ট সহ)।
4. Commit: "chunk D3: whisper cross-check regression pass + edge-case coverage" — push করো।
5. Tag: git tag chunk-D3-done && git push origin chunk-D3-done
```

---
---

## গ্রুপ E — User-facing QA Gate + `app.py` Wiring + Final Regression

### E1 — `pipeline/subtitle_qa.py`: সব diagnostics মিলিয়ে একটা QA summary

```
আমি manhwa-video-dubber-v6-এর subtitle-QA-fix আপডেটে কাজ করছি, GitHub-ভিত্তিক
চেইনের "চাংক E1" (D3 শেষ, গ্রুপ D সম্পূর্ণ)।

Repo: https://github.com/shafin262619-jpg/manhwa-video-dubber-v6.git  (branch: main)

প্রথমে করো:
1. Repo clone/pull করো, tag chunk-D3-done থেকে ভেরিফাই করো।
2. docs/HANDOFF_NEXT.md পড়ো।
3. `pipeline/subtitle_builder.load_subtitle_qa()` (A3) আর
   `pipeline/subtitle_verify.whisper_cross_check()`-এর আউটপুট ফরম্যাট
   (D1, `subtitle_qa_whisper.json`) দুটোই আবার দেখো।

তোমার স্কোপ:

নতুন ফাইল `pipeline/subtitle_qa.py` বানাও:

```python
"""Combined subtitle-extraction QA summary (E1).

Merges the mechanical diagnostics from subtitle_builder (coverage gaps,
duplicate/degenerate-timestamp clusters, repair summary — groups A/B) with
the independent whisper_cross_check verification (group D) into one
human-readable summary the user sees before recording/uploading their
voiceover (group E wiring, E2).
"""

def build_qa_summary(job_id, upload_root=None):
    """subtitle_qa.json (A3, post-repair থেকে B3) আর subtitle_qa_whisper.json
    (D1) দুটোই লোড করে (দুটোই না থাকলে/malformed হলে raise না করে ডিফল্ট
    ধরে নেয়) একটা dict রিটার্ন করে:

        {
            "job_id": job_id,
            "qa_status": "ok" | "flagged",
            "warnings": [<মানুষের পড়ার জন্য বাংলা/ইংরেজি ছোট স্ট্রিং লিস্ট>],
            "gaps_remaining": <count>,
            "duplicate_clusters_remaining": <count>,
            "repair_attempted": <count বা 0>,
            "repair_succeeded": <count বা 0>,
            "whisper_check_status": "ok" | "mismatch" | "skipped",
        }

    qa_status "flagged" হয় যদি (gaps_remaining > 0) বা
    (duplicate_clusters_remaining > 0) বা (whisper_check_status == "mismatch")
    — নাহলে "ok"। warnings লিস্টে প্রতিটা flag-এর জন্য একটা সংক্ষিপ্ত,
    non-technical লাইন থাকবে (যেমন: "~৩২ সেকেন্ডের একটা অংশ হয়তো বাদ পড়ে
    গেছে (serial ৫২-৫৩-এর মাঝে)", "৪টা লাইনে সন্দেহজনক ডুপ্লিকেট টাইমিং
    পাওয়া গেছে")। raise কখনো করবে না।
    """
```

hard constraint: এটা একটা pure aggregation ফাংশন — কোনো নতুন Gemini/Whisper
কল করবে না, শুধু আগে থেকে লেখা দুটো JSON ফাইল পড়ে combine করবে। এই চাংকে
app.py স্পর্শ করবে না — wiring E2-এর কাজ।

টেস্ট (নতুন `pipeline/tests/test_subtitle_qa.py`):
- দুটো ফাইলই clean (কোনো flag নেই) → `qa_status: "ok"`, খালি warnings।
- gaps/duplicate_clusters আছে → `qa_status: "flagged"`, warnings-এ সঠিক
  সংখ্যক/অর্থপূর্ণ লাইন।
- whisper mismatch → `qa_status: "flagged"`, warnings-এ সেটার উল্লেখ।
- একটা বা দুটো ফাইলই missing/malformed → raise না করে reasonable ডিফল্ট
  (`qa_status: "ok"` অথবা যতটুকু তথ্য আছে তা দিয়ে সবচেয়ে যুক্তিসঙ্গত
  স্ট্যাটাস — ঠিক করার সময় নিজে সিদ্ধান্ত নাও, কিন্তু raise কখনো না সেটাই
  মূল constraint, আর যা ডিসিশন নাও সেটা docstring-এ স্পষ্ট লেখো)।

শেষে (Definition of Done):
1. পুরো test suite পাশ করছে কনফার্ম করো:
   `python3 -m unittest discover -s pipeline/tests -v`
2. docs/HANDOFF_NEXT.md: "E1 (subtitle_qa.build_qa_summary, standalone)
   সম্পূর্ণ। পরের কাজ: E2 (app.py-তে ভয়েসওভার আপলোড/রেকর্ডিং শুরুর আগে
   এই summary দেখানো)।"
3. docs/CHANGELOG.md-এ এন্ট্রি যোগ করো।
4. Commit: "chunk E1: pipeline/subtitle_qa.py — combined human-readable QA summary" — push করো।
5. Tag: git tag chunk-E1-done && git push origin chunk-E1-done
```

### E2 — `app.py`-তে wiring: ভয়েসওভার শুরুর আগে QA banner দেখানো (non-blocking)

```
আমি manhwa-video-dubber-v6-এর subtitle-QA-fix আপডেটে কাজ করছি, GitHub-ভিত্তিক
চেইনের "চাংক E2" (E1 শেষ)।

Repo: https://github.com/shafin262619-jpg/manhwa-video-dubber-v6.git  (branch: main)

প্রথমে করো:
1. Repo clone/pull করো, tag chunk-E1-done থেকে ভেরিফাই করো।
2. docs/HANDOFF_NEXT.md পড়ো।
3. app.py-র এই দুটো রুট পড়ো:
   - `voiceover_choose_page(job_id)` (GET `/voiceover/{job_id}/choose`) —
     ইউজার auto_tts বনাম user_upload বেছে নেওয়ার আগে যেটা দেখে।
   - `align_uploaded_page(job_id)` (GET `/voiceover/{job_id}/align_uploaded`) —
     ইতিমধ্যেই একটা `warning` HTML ব্লক আছে যখন alignment fallback হয়,
     সেই স্টাইলটাই অনুসরণ করো।
4. `pipeline/ui.py`-র `ui.page()` হেল্পার দেখো HTML wrap করার কনভেনশনের
   জন্য।

তোমার স্কোপ:

**১.** `voiceover_choose_page(job_id)`-এর ভেতরে, HTML বডি বানানোর আগে:
```python
qa = subtitle_qa.build_qa_summary(job_id)
```
`qa["qa_status"] == "flagged"` হলে একটা warning banner বসাও (ঠিক
`align_uploaded_page`-এর warning ব্লকের স্টাইলে), যাতে থাকবে:
- সংক্ষিপ্ত হেডলাইন (যেমন: "⚠️ এই ভিডিওর সাবটাইটেল এক্সট্র্যাকশনে কিছু
  সমস্যা পাওয়া গেছে")।
- `qa["warnings"]`-এর প্রতিটা লাইন একটা `<li>`।
- একটা লিংক subtitle_qa.json ডাউনলোড করার জন্য (নতুন ডাউনলোড রুট লাগবে,
  নিচে দেখো)।
- স্পষ্ট করে বলা: "এটা informational — তবুও এগিয়ে যেতে পারেন, কিন্তু
  ভয়েসওভার রেকর্ড করার আগে চাইলে সাবটাইটেল দেখে নিতে পারেন।"

`qa_status == "ok"` হলে কোনো banner দেখাবে না (চুপচাপ, বিদ্যমান পেজ যেমন
আছে তেমনই)।

**২.** নতুন ডাউনলোড রুট যোগ করো (বিদ্যমান `download_voiceover_upload`
রুটের ঠিক প্যাটার্নে):
```python
@app.get("/download/{job_id}/subtitle_qa")
def download_subtitle_qa(job_id: str) -> FileResponse:
    path = video_ingest.UPLOAD_ROOT / job_id / "subtitle_qa.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"no subtitle_qa.json for job {job_id}")
    return FileResponse(path, media_type="application/json", filename="subtitle_qa.json")
```

hard constraint:
- **কখনো ব্লক করবে না** — `qa_status: "flagged"` হলেও ইউজার স্বাভাবিকভাবে
  auto_tts/user_upload বেছে এগিয়ে যেতে পারবে, কোনো নতুন confirmation
  ধাপ/ফর্ম যোগ হবে না (এটা শুধু informational banner)।
- `build_qa_summary()` কখনো raise করে না (E1-এর কন্ট্রাক্ট) কিন্তু তবুও
  একটা defensive try/except রাখো এই রুটে — banner বানাতে গিয়ে কোনো
  সমস্যা হলেও `/voiceover/{job_id}/choose` পেজ যেন লোড হতেই থাকে
  (except হলে banner স্কিপ করো, পেজ আগের মতোই দেখাও)।

টেস্ট (`pipeline/tests/test_app_orchestration.py`):
- `qa_status: "ok"` (mocked `build_qa_summary`) হলে `/voiceover/{id}/choose`
  পেজে banner নেই।
- `qa_status: "flagged"` হলে banner আছে, warnings text পেজে দেখা যায়।
- `GET /download/{job_id}/subtitle_qa` — ফাইল থাকলে ২০০ + সঠিক content-type,
  না থাকলে ৪০৪।
- `build_qa_summary` raise করলেও (mocked side_effect) `/voiceover/{id}/choose`
  পেজ তবুও ২০০ স্ট্যাটাস দেয় (banner ছাড়া)।
- বিদ্যমান `test_app_orchestration.py`, `test_full_auto_orchestration.py`
  regression পাশ করে (auto_tts জিরো-ক্লিক পাথ, যেটা `/choose` রুট মোটেও
  কল করে না, সেটা অপ্রভাবিত থাকা কনফার্ম করো)।

শেষে (Definition of Done):
1. পুরো test suite পাশ করছে কনফার্ম করো:
   `python3 -m unittest discover -s pipeline/tests -v`
2. `python3 -m py_compile app.py`
3. docs/HANDOFF_NEXT.md: "E2 (QA banner /voiceover/{id}/choose পেজে,
   /download/{id}/subtitle_qa রুট) সম্পূর্ণ। পরের কাজ: E3 (সম্পূর্ণ
   regression পাস, গ্রুপ A-D সব মিলিয়ে)।"
4. docs/CHANGELOG.md-এ এন্ট্রি যোগ করো।
5. Commit: "chunk E2: wire QA summary banner into /voiceover/{id}/choose (non-blocking) + subtitle_qa.json download route" — push করো।
6. Tag: git tag chunk-E2-done && git push origin chunk-E2-done
```

### E3 — সম্পূর্ণ Regression Pass (গ্রুপ A থেকে E, সব মিলিয়ে)

```
আমি manhwa-video-dubber-v6-এর subtitle-QA-fix আপডেটে কাজ করছি, GitHub-ভিত্তিক
চেইনের "চাংক E3" (E2 শেষ)। এই চাংকের একমাত্র কাজ verification + bugfix —
কোনো নতুন ফিচার না।

Repo: https://github.com/shafin262619-jpg/manhwa-video-dubber-v6.git  (branch: main)

প্রথমে করো:
1. Repo clone/pull করো, tag chunk-E2-done থেকে ভেরিফাই করো।
2. docs/HANDOFF_NEXT.md পড়ো।

তোমার স্কোপ:

1. **পুরো** test suite রান করো — A1 থেকে E2 পর্যন্ত সব যোগ হয়ে যা আছে
   (আগের ২৯০টার ওপর নতুন সব যোগ হয়ে)। যেকোনো regression পেলে ঠিক করো —
   নতুন ফিচার যোগ করবে না, শুধু bugfix।
2. `python3 -m py_compile` দিয়ে touched হওয়া সব ফাইল যাচাই করো: `app.py`,
   `pipeline/config.py`, `pipeline/subtitle_builder.py`,
   `pipeline/subtitle_extract.py`, `pipeline/subtitle_verify.py`,
   `pipeline/subtitle_qa.py`।
3. End-to-end sanity (mocked Gemini/Whisper/ffmpeg, বিদ্যমান
   `test_full_auto_orchestration.py`-র স্টাইলে): auto_tts জিরো-ক্লিক পাথ
   আর user_upload single-pause পাথ — দুটোই এখনো আগের মতো কাজ করে (গ্রুপ
   A-E-এর কোনো wiring যেন এই মূল claim না ভাঙে) নিশ্চিত করো।
4. চূড়ান্ত টেস্ট কাউন্ট আর pass/fail status স্পষ্ট করে ডকুমেন্ট করো।

শেষে (Definition of Done):
1. পুরো test suite ১০০% পাশ করছে (বা কোনটা পাশ করছে না আর কেন, যদি
   সমাধান সম্ভব না হয়, স্পষ্ট করে লেখো)।
2. docs/HANDOFF_NEXT.md: চূড়ান্ত টেস্ট-স্ট্যাটাস, পরের কাজ E4 (final
   wrap-up)।
3. docs/CHANGELOG.md-এ এন্ট্রি যোগ করো।
4. Commit: "chunk E3: full regression pass across groups A-E + fixes" — push করো।
5. Tag: git tag chunk-E3-done && git push origin chunk-E3-done
```

### E4 — Final Wrap-up (সব শেষ, `manhwa-video-dubber-v6-qa-final` ট্যাগ)

```
আমি manhwa-video-dubber-v6-এর subtitle-QA-fix আপডেটে কাজ করছি, GitHub-ভিত্তিক
চেইনের একদম শেষ চাংক "E4" (E3 শেষ, পুরো regression পাশ করেছে)।

Repo: https://github.com/shafin262619-jpg/manhwa-video-dubber-v6.git  (branch: main)

প্রথমে করো:
1. Repo clone/pull করো, tag chunk-E3-done থেকে ভেরিফাই করো।
2. docs/HANDOFF_NEXT.md পড়ো।

তোমার স্কোপ:

1. `docs/FINAL_SUMMARY.md`-তে একটা নতুন সেকশন যোগ করো "## Subtitle QA
   Fixes (A1-E4)", তাতে থাকবে:
   - ৬টা মূল বাগ (এই প্ল্যানের ভূমিকায় লেখা) → কোন গ্রুপ কোনটা ফিক্স
     করেছে তার একটা ম্যাপিং।
   - নতুন ফাইল: `pipeline/subtitle_verify.py`, `pipeline/subtitle_qa.py`।
   - নতুন config constants: `SUBTITLE_GAP_FLAG_THRESHOLD_SEC`,
     `SUBTITLE_DUP_CLUSTER_MIN_COUNT`, `SUBTITLE_MAX_REPAIR_ATTEMPTS`,
     `SUBTITLE_COVERAGE_MISMATCH_RATIO`, আর
     `LONG_VIDEO_CHUNK_THRESHOLD_SEC` 600→90 বদল।
   - নতুন আর্টিফ্যাক্ট ফাইল: `subtitle_qa.json`, `subtitle_qa_whisper.json`
     (উভয়ই `uploads/<job_id>/`-এ)।
   - নতুন রুট: `GET /download/{job_id}/subtitle_qa`।
   - Known limitation: repair single-pass (recursive না), gap/duplicate-cluster
     ছাড়া অন্য ধরনের misplaced-content সমস্যা (যেমন আগের কথোপকথনে পাওয়া
     "hallucination-সন্দেহভাজন অংশ") স্বয়ংক্রিয়ভাবে ধরা পড়ে না — এখনো
     ম্যানুয়াল রিভিউ দরকার, QA banner শুধু signal দেয়।
   - একটা স্পষ্ট নোট: "এই একটা ধাপ কোনো sandboxed AI agent করতে পারবে না
     — ব্যবহারকারীকে নিজে করতে হবে": real Gemini API key + real ~৫-৬
     মিনিটের dialogue-dense ভিডিও দিয়ে পুরো upload → subtitle_qa.json/QA
     banner → (flag থাকলে) manual review → voiceover আপলোড পর্যন্ত একটা
     পূর্ণ রান, যাতে ভেরিফাই হয় (ক) নতুন ৯০-সেকেন্ড chunking বাস্তবেই
     আগের "৫০ সেকেন্ড বাদ পড়া" সমস্যা প্রতিরোধ করছে কিনা, (খ) repair
     mechanism বাস্তব duplicate-cluster-এ কাজ করছে কিনা, (গ) whisper
     cross-check বাস্তব অডিওতে false-positive দিচ্ছে কিনা।
2. docs/HANDOFF_NEXT.md আপডেট করো: "গ্রুপ A-E (subtitle-QA-fix, A1-E4)
   সম্পূর্ণ, বাকি শুধু ব্যবহারকারীর নিজের real-media QA রান।"
3. docs/CHANGELOG.md-এ চূড়ান্ত এন্ট্রি যোগ করো।

শেষে:
1. Commit: "chunk E4: final wrap-up + FINAL_SUMMARY.md subtitle-QA-fix section" — push করো।
2. Tag: git tag manhwa-video-dubber-v6-qa-final && git push origin manhwa-video-dubber-v6-qa-final

যদি context ফুরিয়ে যায় আর কাজ অসম্পূর্ণ থাকে, docs/HANDOFF_NEXT.md-এ
স্পষ্ট করে লিখো কোন অংশ সম্পূর্ণ আর কোনটা না — প্রয়োজনে এই চাংককেও নিজে
আরও ভেঙে (E4-১, E4-২...) পরের সেশনে চালিয়ে যাও, একই প্রোটোকল অনুসরণ করে।
```

---

## সব শেষে — যেটা AI দিয়ে করানো যাবে না

`manhwa-video-dubber-v6-qa-final` ট্যাগ হয়ে গেলেও এই কাজগুলো বাকি থাকবে,
কোনো sandboxed AI agent সেশন করতে পারবে না:

1. **Real Gemini API key + real ~৫-৬ মিনিটের dialogue-dense ভিডিও দিয়ে
   সম্পূর্ণ end-to-end রান** — নেটওয়ার্ক, বড় মিডিয়া ফাইল, real API
   billing লাগে। এটাই সবচেয়ে গুরুত্বপূর্ণ — এই পুরো প্ল্যানটাই একটা
   নির্দিষ্ট real-world ফেইলিওরের ওপর ভিত্তি করে বানানো, তাই আসল ভিডিও
   দিয়ে ভেরিফাই না করলে আপনি নিশ্চিত হতে পারবেন না যে সমস্যাটা আসলেই
   সমাধান হয়েছে কিনা।
2. **Whisper মডেল ডাউনলোড/ইনস্টল** (`openai-whisper` package, প্রায়
   ১৩৯MB `base` মডেল) — sandboxed CI/agent environment-এ নেটওয়ার্ক/ডিস্ক
   সীমাবদ্ধতা থাকতে পারে; group D-এর টেস্ট mocked whisper দিয়ে চলবে,
   কিন্তু real whisper আচরণ (multilingual/Chinese detection accuracy)
   নিজে দেখে নিতে হবে।
3. **QA banner-এর ম্যানুয়াল visual/UX চেক** — `/voiceover/{job_id}/choose`
   পেজে banner আসলে কেমন দেখাচ্ছে (স্টাইলিং, readability) ব্রাউজারে
   নিজের চোখে দেখে কনফার্ম করা।
4. **`subtitle_qa.json`/`subtitle_qa_whisper.json`-এর ওপর ভিত্তি করে
   সিদ্ধান্ত নেওয়া** — flag হওয়া অংশ সত্যিই ভুল কিনা সেটা এখনো একটা
   মানুষকেই ভিডিও দেখে/ট্রান্সক্রিপ্ট মিলিয়ে ভেরিফাই করতে হবে (system
   শুধু কোথায় দেখতে হবে সেটা নির্দেশ করে, ভুলটা নিজে সংশোধন করে না)।

## যদি কোনো চাংক ভুল করে ফেলে

- সংশ্লিষ্ট আগের tag-এ ফিরে যান: `git reset --hard chunk-B2-done` (যেমন)।
- একই প্রম্পট একটা fresh session-এ আবার পেস্ট করুন।
- কোনো চাংক নিজেই বড় মনে হলে, সেই session আরও ছোট ধাপে ভেঙে নিতে পারে —
  একই `docs/HANDOFF_NEXT.md` প্রোটোকল সেটা সামলে নেবে।
