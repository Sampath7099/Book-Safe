# L1 — Query plans on real volume

Dataset: **195,974 bookings**, 500 users, 14 rooms. Hourly non-overlapping
slots per room across the last 730 days, ~80% fill rate.

> Why the volume matters: an index is a claim about **data volume**, not about
> schema. On 14 rooms and 3 bookings, Postgres sequential-scans everything no
> matter what indexes exist, and `EXPLAIN` tells you nothing. This is why the
> `rooms` table has **no index at all** — with 14 rows, a seq scan is genuinely
> the fastest plan, and adding an index would be cargo-cult.

---

## Experiment A — "show me my bookings"

```sql
SELECT id, room_id, starts_at FROM bookings
WHERE user_id = 42 AND status = 'confirmed';
```

### Without the index

```
Gather  (cost=1000.00..5247.08 rows=389) (actual time=0.229..16.001 rows=379)
  -> Parallel Seq Scan on bookings  (actual time=0.031..11.186 rows=190 loops=2)
       Filter: ((user_id = 42) AND (status = 'confirmed'))
       Rows Removed by Filter: 97798
Execution Time: 16.082 ms
```

### With `idx_bookings_user`

```
Bitmap Heap Scan on bookings  (cost=7.32..1046.17) (actual time=0.097..0.633 rows=379)
  -> Bitmap Index Scan on idx_bookings_user  (actual time=0.043..0.043 rows=379)
       Index Cond: (user_id = 42)
Execution Time: 0.672 ms
```

| | Time | Plan |
|---|---|---|
| No index | 16.082 ms | Parallel Seq Scan |
| Index | 0.672 ms | Bitmap Index Scan |
| **Speedup** | **~24×** | |

**How to read it.** `Rows Removed by Filter: 97798` — *per parallel worker*, so
~195k rows examined to return 379. The database read the entire table to find
0.2% of it. Postgres was even forced to parallelise the scan (`Gather`), which
is a sign of desperation, not efficiency.

The index is **partial** (`WHERE status = 'confirmed'`) — it only indexes live
bookings. Cancelled rows are never searched for by user, so indexing them would
waste space and slow down writes.

---

## Experiment B — the overlap check (the availability query's core)

```sql
SELECT EXISTS (
  SELECT 1 FROM bookings b
  WHERE b.room_id = 5 AND b.status = 'confirmed'
    AND tstzrange(b.starts_at, b.ends_at, '[)') && tstzrange($1, $2, '[)')
);
```

### With the GiST index

```
Index Scan using no_overlapping_bookings on bookings b  (actual time=0.682..0.682 rows=1)
  Index Cond: ((room_id = 5) AND (tstzrange(starts_at, ends_at, '[)') && tstzrange(...)))
Execution Time: 0.730 ms
```

### With index scans disabled (`SET enable_indexscan=off; SET enable_bitmapscan=off;`)

```
Seq Scan on bookings b  (actual time=20.759..20.759 rows=1)
  Filter: ((room_id = 5) AND (status = 'confirmed') AND (tstzrange(...) && tstzrange(...)))
  Rows Removed by Filter: 169423
Execution Time: 20.779 ms
```

| | Time | Plan |
|---|---|---|
| Seq scan (forced) | 20.779 ms | Seq Scan, 169,423 rows discarded |
| GiST index | 0.730 ms | Index Scan |
| **Speedup** | **~28×** | |

### The interesting part

**We never created this index.** It was built automatically by the `EXCLUDE`
constraint — a constraint needs an index to enforce itself efficiently, so
declaring the rule gave us the lookup structure for free.

Note the `Index Cond` line: **both** conditions were pushed into the index —
the equality on `room_id` *and* the range overlap. That is exactly what
`btree_gist` buys. Without that extension the constraint could not be created,
because GiST alone has no strategy for integer equality.

Note also this is not a B-tree. A B-tree indexes a **sortable** value, and
"overlapping ranges" have no sort order that puts conflicts adjacent to each
other. GiST is a generalised search tree that indexes ranges by bounding box —
the same family of structure used for geometric and full-text search.

To reproduce, remember to re-enable the planner settings:

```sql
RESET enable_indexscan; RESET enable_bitmapscan;
```

---

## Takeaways

1. Index selection is driven by **row count and selectivity**, not by which
   columns "feel important". `rooms` has none, deliberately.
2. A constraint can be a performance feature. `no_overlapping_bookings` enforces
   correctness *and* accelerates every availability query.
3. `Rows Removed by Filter` is the number to look at first in any plan — it is
   the work the database did and threw away.
4. `EXPLAIN` alone shows estimates; `EXPLAIN ANALYZE` actually runs the query
   and shows reality. When the estimated and actual row counts differ wildly,
   your statistics are stale — run `ANALYZE`.
