"""
Calibration test — checks that p_ai is *meaningful*: clearly-human and clearly-AI
samples land in different classification bands and the score tracks clarity.

Run:  python test_calibration.py
"""

from dotenv import load_dotenv

load_dotenv()

import detection

HUMAN_SAMPLES = [
    "I burned the toast again. Twice. My kitchen smells like a campfire and the "
    "smoke alarm — that screaming little tyrant bolted to the ceiling — will not "
    "shut up. I waved a dish towel at it like an idiot. It kept going. Anyway, "
    "breakfast is cereal now.",
    "Grandpa never said much. But that one afternoon by the lake, fixing the old "
    "motor with grease up to his elbows, he told me the only thing worth knowing: "
    "show up. That's it. Show up, even when it's raining sideways and the fish "
    "aren't biting and you'd rather be anywhere else.",
]

AI_SAMPLES = [
    "In today's rapidly evolving digital landscape, it is important to consider "
    "the various factors that contribute to success. First, organizations must "
    "prioritize innovation. Second, organizations must embrace collaboration. "
    "Third, organizations must leverage technology effectively. By focusing on "
    "these key areas, organizations can achieve sustainable growth and long-term "
    "value for all stakeholders involved in the process.",
    "The system is good. The system is fast. The system is reliable. The system "
    "is good. The system is fast. The system is reliable. The system is good. "
    "The system is fast. The system is reliable.",
]


def report(label, samples):
    print(f"\n=== {label} ===")
    for s in samples:
        r = detection.evaluate_content(s)
        print(
            f"  {r['classification']:<13} p_ai={r['final_p_ai']:<6} "
            f"burst={r['burstiness_score']:<6} rep={r['repetition_score']:<6} "
            f"llm={r['llm_score']}"
        )


if __name__ == "__main__":
    report("HUMAN samples (expect 'Likely Human')", HUMAN_SAMPLES)
    report("AI samples (expect higher p_ai)", AI_SAMPLES)
    print(
        "\nNote: llm_judge abstains at 0.0 when GROQ_API_KEY is unset or the call "
        "fails — a restrictive fallback that can only pull p_ai toward 'human', "
        "never falsely raise it."
    )
