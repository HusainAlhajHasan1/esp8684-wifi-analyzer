package com.hseen.wifianalyzer.protocol

import org.json.JSONArray
import org.json.JSONObject

/**
 * One network as the device measured it. Every field comes from the firmware;
 * nothing here is inferred, defaulted to a plausible value, or invented.
 */
data class Network(
    val ssid: String,
    val ssidHex: String?,
    val bssid: String,
    val rssi: Int,
    val quality: Int,
    val channel: Int,
    val freqMhz: Int,
    val band: String,
    val widthMhz: Int,
    val security: String,
    val hidden: Boolean,
    val wps: Boolean,
    val phy: List<String>,
    val flags: List<String>,
) {
    val isWeak: Boolean get() = flags.contains("weak")
    val isStrong: Boolean get() = flags.contains("strong")
    fun hasFlag(f: String): Boolean = flags.contains(f)

    companion object {
        private fun JSONArray.toStringList(): List<String> =
            (0 until length()).map { optString(it, "") }

        fun fromJson(o: JSONObject): Network = Network(
            ssid = o.optString("ssid", ""),
            ssidHex = if (o.has("ssid_hex") && !o.isNull("ssid_hex")) o.optString("ssid_hex") else null,
            bssid = o.optString("bssid", ""),
            rssi = o.optInt("rssi", 0),
            quality = o.optInt("quality", 0),
            channel = o.optInt("channel", 0),
            freqMhz = o.optInt("freq_mhz", 0),
            band = o.optString("band", ""),
            widthMhz = o.optInt("width_mhz", 0),
            security = o.optString("security", "unknown"),
            hidden = o.optBoolean("hidden", false),
            wps = o.optBoolean("wps", false),
            phy = o.optJSONArray("phy")?.toStringList() ?: emptyList(),
            flags = o.optJSONArray("flags")?.toStringList() ?: emptyList(),
        )
    }
}

/** One page of one sweep. `pages` is how many pages the device will send. */
data class ScanPage(
    val scanId: Int,
    val page: Int,
    val pages: Int,
    val total: Int,
    val truncated: Boolean,
    val dropped: Int,
    val scanMs: Int,
    val cached: Boolean,
    val networks: List<Network>,
) {
    companion object {
        fun fromJson(d: JSONObject): ScanPage {
            val arr = d.optJSONArray("networks") ?: JSONArray()
            val nets = (0 until arr.length()).mapNotNull { i ->
                arr.optJSONObject(i)?.let { Network.fromJson(it) }
            }
            return ScanPage(
                scanId = d.optInt("scan_id", 0),
                page = d.optInt("page", 1),
                pages = d.optInt("pages", 1),
                total = d.optInt("total", 0),
                truncated = d.optBoolean("truncated", false),
                dropped = d.optInt("dropped", 0),
                scanMs = d.optInt("scan_ms", 0),
                cached = d.optBoolean("cached", false),
                networks = nets,
            )
        }
    }
}

/** A fully reassembled sweep. */
data class Sweep(
    val scanId: Int,
    val networks: List<Network>,
    val total: Int,
    val pages: Int,
    val truncated: Boolean,
    val dropped: Int,
    val scanMs: Int,
    val cached: Boolean,
    val violations: List<String>,
    val notes: List<String>,
)

/** Anything the device sent, already parsed. Unknown types are kept, not dropped. */
data class Frame(
    val type: String,
    val id: Int,
    val seq: Int,
    val data: JSONObject,
)
