from typing import List, Dict

def reciprocal_rank_fusion(hybrid_lists: List[List[Dict]], k: int = 5) -> List[Dict]:
    rank_scores = {}
    for l in hybrid_lists:
        for rank, item in enumerate(l):
            pid = item.get('rowid') or item.get('id')
            if not pid: continue
            score = 1.0 / (60 + rank)
            if pid not in rank_scores:
                rank_scores[pid] = [0.0, item]
            rank_scores[pid][0] += score
    fused = sorted(rank_scores.values(), key=lambda x: (-x[0], str(x[1].get('file_path',''))))
    return [x[1] for x in fused[:k]]
