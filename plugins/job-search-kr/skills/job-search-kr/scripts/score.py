#!/usr/bin/env python3
"""공고 평가 점수를 계산한다. 항목 점수는 에이전트가 매기고, 계산만 여기서 한다.

인자를 받지 않는다. 읽는 파일은 항상 work/score.json 이다.

{
  "scores": {"요건 충족": 4, "목표 적합": 3, "보상·조건": 3, "일하는 방식": 3, "회사 상태": 3},
  "deductions": [{"reason": "용역 문구", "points": 0.5}]
}

점수는 1~5 정수. 감점은 합쳐서 1.0점을 넘지 않는다.
"""
import sys

from _common import WORK, UserError, load_json

WEIGHTS = {"요건 충족": 0.30, "목표 적합": 0.25, "보상·조건": 0.20, "일하는 방식": 0.15, "회사 상태": 0.10}
BANDS = [
    (4.5, "우선 지원"),
    (4.0, "지원할 만함"),
    (3.5, "특별한 이유가 있을 때만"),
    (0.0, "지원하지 않기를 권함"),
]
MAX_DEDUCTION = 1.0


def main():
    path = WORK / "score.json"
    if path.is_symlink() or not path.is_file():
        raise UserError(
            'work/score.json 파일이 없습니다. 형식: {"scores": {"요건 충족": 4, "목표 적합": 3, "보상·조건": 3, '
            '"일하는 방식": 3, "회사 상태": 3}, "deductions": [{"reason": "용역 문구", "points": 0.5}]}'
        )
    data = load_json(path)
    scores = data.get("scores") or {}
    if set(scores) != set(WEIGHTS):
        raise UserError("scores 에는 다섯 항목이 정확히 있어야 합니다: " + ", ".join(WEIGHTS))
    for name, value in scores.items():
        if not isinstance(value, int) or not 1 <= value <= 5:
            raise UserError(f"{name} 점수는 1~5 정수여야 합니다: {value!r}")

    deductions = data.get("deductions") or []
    total_deduction = 0.0
    for item in deductions:
        points = item.get("points") if isinstance(item, dict) else None
        if not isinstance(points, (int, float)) or points < 0 or not item.get("reason"):
            raise UserError(f"감점 항목에는 reason 과 0 이상의 points 가 필요합니다: {item!r}")
        total_deduction += points
    if total_deduction > MAX_DEDUCTION + 1e-9:
        raise UserError(f"감점 합계 {total_deduction:.1f}점이 한도 {MAX_DEDUCTION:.1f}점을 넘습니다.")

    weighted = sum(scores[name] * weight for name, weight in WEIGHTS.items())
    final = round(weighted - total_deduction + 1e-9, 1)
    band = next(label for floor, label in BANDS if final >= floor)

    print(f"가중 평균: {weighted:.2f}")
    for item in deductions:
        print(f"감점: -{item['points']:.1f} ({item['reason']})")
    print(f"점수: {final:.1f} / 5")
    print(f"해석: {band}")
    if final >= 4.5 and scores["일하는 방식"] <= 2:
        print("경고: 총점은 높지만 일하는 방식이 2점 이하입니다.")


if __name__ == "__main__":
    try:
        main()
    except UserError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        sys.exit(1)
