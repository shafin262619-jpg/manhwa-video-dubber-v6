"""Stage metadata for the F10 progress bar.

The progress bar and per-stage rows on the polling page are built from this
single source of truth: the ordered ``STAGE_SEQUENCE`` (one slot per major
pipeline milestone) plus the Bengali labels shown to the user.

Not every slot maps 1:1 to a status-file stage name — the D2 (auto TTS) and
D3 (user-upload alignment) voiceover stages share one slot, and the
``final_render`` umbrella stage owns the F3 slot when no ``F3_final`` sub-stage
entry exists. ``STAGE_KEY_GROUPS`` and ``UMBRELLA_TO_SEQUENCE`` encode those
mappings for the client-side poll loop.
"""

STAGE_SEQUENCE = [
    "F1_extract",
    "C1_translate",
    "D2_voiceover_or_D3_align",
    "D4_unify",
    "E1_guideline",
    "E2_draft",
    "F3_final",
]

STAGE_LABELS_BN = {
    "F1_extract": "সাবটাইটেল বের করা হচ্ছে",
    "C1_translate": "অনুবাদ হচ্ছে",
    "D2_voiceover_or_D3_align": "ভয়েসওভার প্রসেস হচ্ছে",
    "D4_unify": "টাইমিং মেলানো হচ্ছে",
    "E1_guideline": "এডিট গাইডলাইন তৈরি হচ্ছে",
    "E2_draft": "ড্রাফট রেন্ডার হচ্ছে",
    "F3_final": "ফাইনাল ভিডিও তৈরি হচ্ছে",
}

# Status-file stage keys that fill each progress-bar slot. Multiple keys =
# alternatives (the slot advances when ANY of them is done).
STAGE_KEY_GROUPS = {
    "F1_extract": ("F1_extract",),
    "C1_translate": ("C1_translate",),
    "D2_voiceover_or_D3_align": ("D2_voiceover", "D3_align"),
    "D4_unify": ("D4_unify",),
    "E1_guideline": ("E1_guideline",),
    "E2_draft": ("E2_draft",),
    "F3_final": ("F3_final",),
}

# Umbrella stages that own a progress-bar slot but write no matching sub-stage
# entry. The slot is shown as "running" while such a stage runs.
UMBRELLA_TO_SEQUENCE = {
    "final_render": "F3_final",
}
