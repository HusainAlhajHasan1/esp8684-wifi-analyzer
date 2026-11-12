package com.hseen.wifianalyzer

import android.content.Intent
import android.hardware.usb.UsbManager
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import com.hseen.wifianalyzer.ui.ScanScreen
import com.hseen.wifianalyzer.ui.ScanViewModel
import com.hseen.wifianalyzer.ui.WifiAnalyzerTheme

class MainActivity : ComponentActivity() {

    private val vm: ScanViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent { WifiAnalyzerTheme { ScanScreen(vm) } }
        // Plugging the board in launches the app with USB_DEVICE_ATTACHED.
        if (intent?.action == UsbManager.ACTION_USB_DEVICE_ATTACHED) vm.connect()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        if (intent.action == UsbManager.ACTION_USB_DEVICE_ATTACHED) vm.connect()
    }
}
