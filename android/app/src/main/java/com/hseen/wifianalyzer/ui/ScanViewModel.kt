package com.hseen.wifianalyzer.ui

import android.app.Application
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.hseen.wifianalyzer.protocol.Frame
import com.hseen.wifianalyzer.protocol.Network
import com.hseen.wifianalyzer.protocol.ScanCollector
import com.hseen.wifianalyzer.protocol.ScanPage
import com.hseen.wifianalyzer.protocol.Sweep
import com.hseen.wifianalyzer.usb.UsbSerialLink
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.json.JSONObject

class ScanViewModel(app: Application) : AndroidViewModel(app) {

    private val link = UsbSerialLink(app.applicationContext)
    private val collector = ScanCollector()
    private var requestId = 1
    private var handshakeJob: Job? = null
    private var timeoutJob: Job? = null

    // --- observable state ---------------------------------------------------
    var connection by mutableStateOf("no device")
        private set
    var deviceLine by mutableStateOf<String?>(null)
        private set
    var firmware by mutableStateOf<String?>(null)
        private set
    var scanning by mutableStateOf(false)
        private set
    var networks by mutableStateOf<List<Network>>(emptyList())
        private set
    var lastSweep by mutableStateOf<Sweep?>(null)
        private set
    var status by mutableStateOf("Plug in the board, then press Connect.")
        private set
    var violations by mutableStateOf<List<String>>(emptyList())
        private set
    var notes by mutableStateOf<List<String>>(emptyList())
        private set
    var framesSeen by mutableStateOf(0)
        private set

    init {
        link.onLine = ::onLine
        link.onState = { state ->
            connection = when (state) {
                is UsbSerialLink.State.NoDevice -> "no device"
                is UsbSerialLink.State.PermissionDenied -> "permission denied"
                is UsbSerialLink.State.Failed -> "error"
                is UsbSerialLink.State.Connected -> "connected (${state.bridge})"
            }
            when (state) {
                is UsbSerialLink.State.Connected -> beginHandshake()
                is UsbSerialLink.State.NoDevice ->
                    status = "No USB-serial device found. Check the cable and the CH340 driver."
                is UsbSerialLink.State.PermissionDenied ->
                    status = "USB permission was denied - press Connect and allow it."
                is UsbSerialLink.State.Failed -> status = "Link error: ${state.message}"
            }
        }
    }

    fun connect() = link.connect()

    fun disconnect() {
        handshakeJob?.cancel()
        timeoutJob?.cancel()
        link.close()
        connection = "no device"
        scanning = false
        status = "Disconnected."
    }

    /**
     * Opening the port resets the ESP, so the first frames are boot noise at a
     * different crystal-scaled baud in the worst case. Retry `hello` instead of
     * giving up on the first silence.
     */
    private fun beginHandshake() {
        handshakeJob?.cancel()
        handshakeJob = viewModelScope.launch {
            repeat(10) { attempt ->
                send("hello")
                delay(400)
                if (deviceLine != null) return@launch
                if (attempt == 9) status = "Board is not answering hello - is the firmware v0.2.x image flashed?"
            }
        }
    }

    fun scan() {
        if (scanning) return
        collector.reset()
        scanning = true
        status = "Scanning - a full 2.4 GHz sweep takes ~2 s..."
        send("scan.start", JSONObject().put("continuous", false))
        // If the sweep never completes, say which pages are missing. Never pad
        // the list with a partial result presented as complete.
        timeoutJob?.cancel()
        timeoutJob = viewModelScope.launch {
            delay(12_000)
            if (scanning) {
                scanning = false
                status = lastSweep?.let { "Sweep finished." }
                    ?: "No sweep arrived. The device reported nothing - nothing was invented to fill the gap."
            }
        }
    }

    /** Re-request the cached sweep from the device, no new RF activity. */
    fun replayCache() {
        send("scan.get", JSONObject().put("replay", true))
        status = "Asked the device to replay its cached sweep..."
    }

    private fun send(cmd: String, params: JSONObject? = null) {
        val obj = JSONObject()
            .put("v", 1)
            .put("id", requestId++)
            .put("cmd", cmd)
        if (params != null) obj.put("params", params)
        link.send(obj.toString())
    }

    private fun onLine(line: String) {
        val obj = runCatching { JSONObject(line) }.getOrNull() ?: return
        val type = obj.optString("type").ifEmpty { return }
        if (obj.optInt("v", 1) != 1) return
        framesSeen++
        val frame = Frame(type, obj.optInt("id", 0), obj.optInt("seq", 0), obj.optJSONObject("data") ?: JSONObject())

        when (type) {
            "hello" -> {
                val d = frame.data
                deviceLine = d.optString("device", "unknown")
                firmware = d.optString("fw", "?")
                status = "Handshake OK - $deviceLine, firmware $firmware"
            }
            "scan_started" -> status = "Sweep acknowledged (scan ${frame.data.optInt("scan_id")})..."
            "scan_result" -> {
                val page = ScanPage.fromJson(frame.data)
                val sweep = collector.onPage(page)
                if (sweep != null) apply(sweep)
                else status = "Page ${page.page} of ${page.pages} received..."
            }
            "scan_status" -> {
                val count = frame.data.optInt("networks", 0)
                status = "Device reports $count cached network(s), ${frame.data.optInt("scans", 0)} scan(s) so far"
            }
            "error" -> {
                val code = obj.optString("code", frame.data.optString("code", "?"))
                val msg = obj.optString("msg", frame.data.optString("msg", ""))
                scanning = false
                status = "Device error $code: $msg"
            }
            "event.heartbeat" -> Unit
            else -> Unit
        }
    }

    private fun apply(sweep: Sweep) {
        timeoutJob?.cancel()
        scanning = false
        networks = sweep.networks.sortedByDescending { it.rssi }
        lastSweep = sweep
        violations = sweep.violations
        notes = sweep.notes
        status = buildString {
            append("${sweep.networks.size}/${sweep.total} networks")
            append(" - ${sweep.pages} page(s)")
            append(" - ${sweep.scanMs} ms")
            if (sweep.truncated) append(" - truncated, radio saw more")
            if (sweep.dropped > 0) append(" - ${sweep.dropped} dropped")
            if (sweep.cached) append(" - from device cache")
        }
    }

    override fun onCleared() {
        link.close()
        super.onCleared()
    }
}
