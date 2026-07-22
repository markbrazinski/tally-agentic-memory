"""Generate 300 random L2-normalized 1024-d vectors for Session D0 smoke test S3/S6.

C-SPANN preview is Euclidean-only; normalization makes cosine-equivalent
comparisons valid under L2 distance, so it's load-bearing, not cosmetic.
"""
import json
import random
import sys

DIM = 1024
COUNT = 300


def gen_unit_vector(dim: int) -> list[float]:
    v = [random.gauss(0.0, 1.0) for _ in range(dim)]
    norm = sum(x * x for x in v) ** 0.5
    return [x / norm for x in v]


def main() -> None:
    random.seed(541)
    vectors = [gen_unit_vector(DIM) for _ in range(COUNT)]
    json.dump(vectors, sys.stdout)


if __name__ == "__main__":
    main()
