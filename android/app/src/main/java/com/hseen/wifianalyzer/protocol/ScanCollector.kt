package com.hseen.wifianalyzer.protocol

/**
 * Reassembles paged sweeps.
 *
 * History worth keeping: the Python host first collected a sweep by re-scanning
 * the buffered stream from the oldest frame on every call, so page 1 was read
 * three times and pages 2-3 were never seen - a real 13-network sweep was
 * recorded as 5 networks, with the device blamed for it. The firmware was
 * right. So here:
 *
 *   * pages are keyed by (scan_id, page) and appended only once;
 *   * a sweep is emitted only when every expected page is present;
 *   * an incomplete sweep reports exactly which pages are missing rather than
 *     quietly returning fewer networks.
 */
class ScanCollector {

    private val pagesByScan = mutableMapOf<Int, MutableMap<Int, ScanPage>>()
    private var lastPages = mutableMapOf<Int, Int>()

    fun reset() {
        pagesByScan.clear()
        lastPages.clear()
    }

    /** Returns a [Sweep] as soon as the sweep is complete, otherwise null. */
    fun onPage(p: ScanPage): Sweep? {
        val pages = pagesByScan.getOrPut(p.scanId) { mutableMapOf() }
        lastPages[p.scanId] = p.pages

        // A replayed page (scan.get replay) must not duplicate networks.
        if (pages.containsKey(p.page)) return null
        pages[p.page] = p

        val expected = p.pages.coerceAtLeast(1)
        if (pages.size < expected) return null

        val missing = (1..expected).filterNot { pages.containsKey(it) }
        val networks = pages.entries
            .sortedBy { it.key }
            .flatMap { it.value.networks }

        val violations = mutableListOf<String>()
        val notes = mutableListOf<String>()

        if (missing.isNotEmpty()) {
            violations += "only ${pages.size} of $expected page(s) arrived (missing $missing)"
        }
        if (p.total != networks.size) {
            violations += "total=${p.total} but ${networks.size} network objects arrived"
        }
        val dupes = networks.groupingBy { it.bssid }.eachCount().filterValues { it > 1 }
        if (dupes.isNotEmpty()) {
            violations += "duplicate BSSID(s): ${dupes.keys.joinToString()}"
        }
        networks.forEach { n ->
            Contract.violations(n).forEach { violations += "${n.bssid}: $it" }
            Contract.knownDeviation(n)?.let { notes += "${n.bssid}: $it" }
        }

        val sweep = Sweep(
            scanId = p.scanId,
            networks = networks,
            total = p.total,
            pages = expected,
            truncated = p.truncated,
            dropped = p.dropped,
            scanMs = p.scanMs,
            cached = p.cached,
            violations = violations,
            notes = notes,
        )
        pagesByScan.remove(p.scanId)
        return sweep
    }

    /** What is still outstanding, for a timeout message that tells the truth. */
    fun pending(scanId: Int): String? {
        val got = pagesByScan[scanId] ?: return null
        val expected = lastPages[scanId] ?: return null
        val missing = (1..expected).filterNot { got.containsKey(it) }
        return if (missing.isEmpty()) null
        else "sweep $scanId incomplete: ${got.size}/$expected pages, missing $missing"
    }
}
