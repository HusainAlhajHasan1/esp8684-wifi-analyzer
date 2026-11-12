package com.hseen.wifianalyzer.protocol

/**
 * The contract, executable.
 *
 * This is a Kotlin mirror of `tools/host/serial_probe.py` (the reference
 * implementation) and of `docs/PROTOCOL.md` section 7. It exists so the phone
 * cannot drift from the firmware the way the host once did: on real hardware a
 * `wpa2/wpa3` network arrived flagged `strong` while the spec excluded it, and
 * nothing checked the flag at all.
 */
object Contract {

    /**
     * `strong` means WPA3-CAPABLE, not WPA3-enforced. A wpa2/wpa3 AP is in
     * transition mode and still admits WPA2-PSK clients, so this flag is never
     * rendered as "WPA3" or as a security guarantee in the UI.
     */
    val WPA3_CAPABLE: Set<String> = setOf(
        "wpa3", "wpa2/wpa3", "wpa3-192",
        "wpa3-enterprise", "wpa2/wpa3-enterprise", "owe",
    )

    /** Firmware v0.2.1 does not flag the enterprise WPA3 modes. Documented, not excused. */
    val KNOWN_V021_STRONG_GAP: Set<String> = setOf("wpa3-enterprise", "wpa2/wpa3-enterprise")

    val SECURITY_VALUES: Set<String> = setOf(
        "open", "wep", "wpa", "wpa2", "wpa/wpa2", "wpa2/wpa3", "wpa3", "wpa3-192",
        "owe", "wapi", "unknown", "wpa2-enterprise", "wpa3-enterprise",
        "wpa2/wpa3-enterprise", "wpa-enterprise",
    )

    fun qualityFromRssi(rssi: Int): Int = (2 * (rssi + 100)).coerceIn(0, 100)

    fun expectedFreq(channel: Int): Int = if (channel == 14) 2484 else 2407 + 5 * channel

    /** Hard violations. Anything non-empty means the device contradicted its own spec. */
    fun violations(n: Network): List<String> {
        val bad = mutableListOf<String>()

        if (n.bssid.length != 17 || n.bssid.count { it == ':' } != 5) {
            bad += "bssid malformed: ${n.bssid}"
        }
        if (n.channel !in 1..14) {
            bad += "channel out of range: ${n.channel}"
        } else if (n.freqMhz != expectedFreq(n.channel)) {
            bad += "freq_mhz ${n.freqMhz} != ${expectedFreq(n.channel)} for channel ${n.channel}"
        }
        if (n.rssi !in -110..10) {
            bad += "rssi implausible: ${n.rssi}"
        } else if (n.quality != qualityFromRssi(n.rssi)) {
            bad += "quality ${n.quality} != ${qualityFromRssi(n.rssi)} for rssi ${n.rssi}"
        }
        if (n.band != "2.4ghz") bad += "band must be 2.4ghz, got ${n.band}"
        if (n.widthMhz !in listOf(20, 40)) bad += "width_mhz not 20/40: ${n.widthMhz}"
        if (n.security !in SECURITY_VALUES) bad += "security not in contract: ${n.security}"

        // The honesty rule: a hidden network must never acquire a name.
        if (n.hidden) {
            if (n.ssid.isNotEmpty()) bad += "hidden network carries an SSID (invented data)"
            if (!n.ssidHex.isNullOrEmpty()) bad += "hidden network carries ssid_hex"
        } else if (n.ssid.isEmpty() && n.ssidHex.isNullOrEmpty()) {
            bad += "visible network has neither ssid nor ssid_hex"
        }

        val expectWeak = n.security == "open" || n.wps
        if (n.isWeak != expectWeak) {
            bad += "weak flag ${n.isWeak} inconsistent with open=${n.security == "open"} wps=${n.wps}"
        }
        if (n.hasFlag("wps") != n.wps) bad += "wps flag inconsistent with wps field"
        if (n.hasFlag("hidden") != n.hidden) bad += "hidden flag inconsistent with hidden field"
        if (n.hasFlag("open") != (n.security == "open")) bad += "open flag inconsistent with security"

        val wantsStrong = n.security in WPA3_CAPABLE
        if (wantsStrong != n.isStrong) {
            if (!(wantsStrong && n.security in KNOWN_V021_STRONG_GAP)) {
                bad += "strong flag ${n.isStrong} inconsistent with security '${n.security}'"
            }
        }
        return bad
    }

    /** A deviation we already understand and accept. Never a violation. */
    fun knownDeviation(n: Network): String? =
        if (n.security in KNOWN_V021_STRONG_GAP && !n.isStrong) {
            "WPA3-Enterprise without the strong flag - known firmware v0.2.1 deviation"
        } else null

    // ---- display rules -----------------------------------------------------

    /**
     * What to show as the network name.
     *
     * A hidden network has no name and this app will not make one up: the UI
     * says so explicitly. (The device could only ever learn it from a probe
     * response, which most APs do not answer.)
     */
    fun displayName(n: Network): String = when {
        n.ssidHex != null -> decodeHexSsid(n.ssidHex) ?: "[non-UTF-8 SSID]"
        n.hidden || n.ssid.isEmpty() -> HIDDEN_LABEL
        else -> n.ssid
    }

    /** Clearly a UI label, never a value that could be mistaken for data. */
    const val HIDDEN_LABEL = "\u2039hidden network\u203A"

    /** `strong` is presented as capability, never as a guarantee. */
    fun strongLabel(): String = "WPA3 capable"

    fun decodeHexSsid(hex: String): String? = runCatching {
        val bytes = ByteArray(hex.length / 2) { i ->
            hex.substring(i * 2, i * 2 + 2).toInt(16).toByte()
        }
        val s = String(bytes, Charsets.UTF_8)
        // If it decoded to replacement characters it was not UTF-8 after all.
        if (s.contains('\uFFFD')) null else s
    }.getOrNull()
}
