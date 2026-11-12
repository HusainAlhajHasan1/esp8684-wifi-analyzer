package com.hseen.wifianalyzer.usb

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbManager
import android.os.Build
import com.hoho.android.usbserial.driver.UsbSerialPort
import com.hoho.android.usbserial.driver.UsbSerialProber
import java.io.ByteArrayOutputStream
import java.util.concurrent.atomic.AtomicBoolean

/**
 * USB-serial transport for the analyzer board.
 *
 * Deliberately dumb: it opens the port, hands complete lines to the parser and
 * writes newline-terminated JSON. It never interprets a frame, so the protocol
 * layer stays testable without hardware.
 */
class UsbSerialLink(private val context: Context) {

    sealed interface State {
        data object NoDevice : State
        data class PermissionDenied(val deviceName: String) : State
        data class Connected(val deviceName: String, val bridge: String) : State
        data class Failed(val message: String) : State
    }

    var onLine: ((String) -> Unit)? = null
    var onState: ((State) -> Unit)? = null

    private val usbManager = context.getSystemService(Context.USB_SERVICE) as UsbManager
    private var port: UsbSerialPort? = null
    private var reader: Thread? = null
    private val running = AtomicBoolean(false)
    private val buffer = ByteArrayOutputStream()
    private var registered = false

    private val permissionReceiver = object : BroadcastReceiver() {
        override fun onReceive(ctx: Context, intent: Intent) {
            if (intent.action != ACTION_USB_PERMISSION) return
            val device: UsbDevice? = if (Build.VERSION.SDK_INT >= 33) {
                intent.getParcelableExtra(UsbManager.EXTRA_DEVICE, UsbDevice::class.java)
            } else {
                @Suppress("DEPRECATION")
                intent.getParcelableExtra(UsbManager.EXTRA_DEVICE)
            }
            val granted = intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false)
            if (granted && device != null) open(device)
            else onState?.invoke(State.PermissionDenied(device?.deviceName ?: "unknown"))
        }
    }

    /**
     * Find a board, ask for permission if needed, then open it.
     *
     * Opening asserts DTR/RTS on most bridges, which resets the ESP: the first
     * ~1.5 s of output is boot noise, and the handshake must be retried rather
     * than assumed. That is handled in the view model, not here.
     */
    fun connect() {
        if (!registered) {
            val filter = IntentFilter(ACTION_USB_PERMISSION)
            if (Build.VERSION.SDK_INT >= 33) {
                context.registerReceiver(permissionReceiver, filter, Context.RECEIVER_NOT_EXPORTED)
            } else {
                @Suppress("UnspecifiedRegisterReceiverFlag")
                context.registerReceiver(permissionReceiver, filter)
            }
            registered = true
        }

        val device = usbManager.deviceList.values.firstOrNull { dev ->
            UsbSerialProber.getDefaultProber().probeDevice(dev) != null
        }
        if (device == null) {
            onState?.invoke(State.NoDevice)
            return
        }
        if (!usbManager.hasPermission(device)) {
            val flags = if (Build.VERSION.SDK_INT >= 31) PendingIntent.FLAG_MUTABLE else 0
            val intent = PendingIntent.getBroadcast(
                context, 0, Intent(ACTION_USB_PERMISSION).setPackage(context.packageName), flags,
            )
            usbManager.requestPermission(device, intent)
            return
        }
        open(device)
    }

    private fun open(device: UsbDevice) {
        try {
            val driver = UsbSerialProber.getDefaultProber().probeDevice(device)
            if (driver == null) {
                onState?.invoke(State.Failed("no USB-serial driver for ${device.deviceName}"))
                return
            }
            val connection = usbManager.openDevice(device)
            if (connection == null) {
                onState?.invoke(State.Failed("could not open ${device.deviceName}"))
                return
            }
            val p = driver.ports.first()
            p.open(connection)
            p.setParameters(115200, 8, UsbSerialPort.STOPBITS_1, UsbSerialPort.PARITY_NONE)
            // Do not touch DTR/RTS beyond what open() did: on boards where RTS
            // is wired to EN, toggling it mid-session reboots the device.
            port = p
            running.set(true)
            reader = Thread({ readLoop(p) }, "usb-reader").apply { isDaemon = true; start() }
            onState?.invoke(State.Connected(device.deviceName, driver.javaClass.simpleName))
        } catch (t: Throwable) {
            onState?.invoke(State.Failed("open failed: ${t.message}"))
        }
    }

    private fun readLoop(p: UsbSerialPort) {
        val chunk = ByteArray(512)
        while (running.get()) {
            try {
                val n = p.read(chunk, 500)
                if (n <= 0) continue
                synchronized(buffer) {
                    for (i in 0 until n) {
                        val b = chunk[i]
                        if (b == '\n'.code.toByte()) {
                            val line = buffer.toString("UTF-8").trim()
                            buffer.reset()
                            if (line.isNotEmpty()) onLine?.invoke(line)
                        } else if (b != '\r'.code.toByte()) {
                            buffer.write(b.toInt())
                            if (buffer.size() > MAX_LINE) buffer.reset() // never grow unbounded
                        }
                    }
                }
            } catch (_: Throwable) {
                if (running.get()) onState?.invoke(State.Failed("link lost"))
                return
            }
        }
    }

    fun send(line: String) {
        val p = port ?: return
        try {
            p.write(line.toByteArray(Charsets.UTF_8), WRITE_TIMEOUT_MS)
            p.write(byteArrayOf('\n'.code.toByte()), WRITE_TIMEOUT_MS)
        } catch (t: Throwable) {
            onState?.invoke(State.Failed("write failed: ${t.message}"))
        }
    }

    fun close() {
        running.set(false)
        reader?.interrupt()
        reader = null
        try {
            port?.close()
        } catch (_: Throwable) {
        }
        port = null
        if (registered) {
            try {
                context.unregisterReceiver(permissionReceiver)
            } catch (_: Throwable) {
            }
            registered = false
        }
    }

    companion object {
        private const val ACTION_USB_PERMISSION = "com.hseen.wifianalyzer.USB_PERMISSION"
        private const val MAX_LINE = 8 * 1024
        private const val WRITE_TIMEOUT_MS = 2000
    }
}
