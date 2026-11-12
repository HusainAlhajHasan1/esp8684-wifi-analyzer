package com.hseen.wifianalyzer.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.hseen.wifianalyzer.protocol.Contract
import com.hseen.wifianalyzer.protocol.Network

enum class NetFilter(val label: String) {
    ALL("All"), WEAK("Weak"), WPS("WPS advertised"), OPEN("Open"),
    HIDDEN("Hidden"), CAPABLE("WPA3 capable"),
}

enum class NetSort(val label: String) { RSSI("Signal"), CHANNEL("Channel"), NAME("Name") }

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ScanScreen(vm: ScanViewModel = viewModel()) {
    var filter by remember { mutableStateOf(NetFilter.ALL) }
    var sort by remember { mutableStateOf(NetSort.RSSI) }
    var selected by remember { mutableStateOf<Network?>(null) }

    val shown = remember(vm.networks, filter, sort) {
        val filtered = vm.networks.filter { n ->
            when (filter) {
                NetFilter.ALL -> true
                NetFilter.WEAK -> n.isWeak
                NetFilter.WPS -> n.wps
                NetFilter.OPEN -> n.security == "open"
                NetFilter.HIDDEN -> n.hidden
                NetFilter.CAPABLE -> n.isStrong
            }
        }
        when (sort) {
            NetSort.RSSI -> filtered.sortedByDescending { it.rssi }
            NetSort.CHANNEL -> filtered.sortedBy { it.channel }
            NetSort.NAME -> filtered.sortedBy { Contract.displayName(it).lowercase() }
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Wi-Fi Analyzer") },
                actions = {
                    Text(
                        vm.connection,
                        fontSize = 12.sp,
                        color = if (vm.connection.startsWith("connected")) StrongColor else MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(end = 12.dp),
                    )
                },
            )
        },
    ) { pad ->
        Column(Modifier.padding(pad).fillMaxWidth()) {

            Card(
                Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 6.dp),
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant),
            ) {
                Column(Modifier.padding(12.dp)) {
                    Text(vm.status, fontSize = 13.sp)
                    if (vm.deviceLine != null) {
                        Text(
                            "${vm.deviceLine} - firmware ${vm.firmware}",
                            fontSize = 11.sp,
                            fontFamily = FontFamily.Monospace,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                    Spacer(Modifier.height(8.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Button(onClick = { vm.connect() }) { Text("Connect") }
                        Button(onClick = { vm.scan() }, enabled = !vm.scanning) {
                            Text(if (vm.scanning) "Scanning..." else "Scan")
                        }
                        OutlinedButton(onClick = { vm.replayCache() }) { Text("Replay cache") }
                    }
                }
            }

            // A contract violation is never swallowed: it means the device
            // contradicted its own specification and the numbers cannot be trusted.
            if (vm.violations.isNotEmpty()) {
                Card(
                    Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp),
                    colors = CardDefaults.cardColors(containerColor = ViolationColor.copy(alpha = 0.15f)),
                ) {
                    Column(Modifier.padding(12.dp)) {
                        Text("Contract violations (${vm.violations.size})", fontWeight = FontWeight.Bold, fontSize = 13.sp)
                        vm.violations.take(4).forEach { Text("- $it", fontSize = 11.sp) }
                        if (vm.violations.size > 4) Text("... and ${vm.violations.size - 4} more", fontSize = 11.sp)
                    }
                }
            }
            vm.notes.takeIf { it.isNotEmpty() }?.let { notes ->
                Card(
                    Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp),
                    colors = CardDefaults.cardColors(containerColor = WeakColor.copy(alpha = 0.12f)),
                ) {
                    Column(Modifier.padding(12.dp)) {
                        Text("Known deviation noted (${notes.size})", fontSize = 12.sp, fontWeight = FontWeight.Bold)
                        Text(notes.first(), fontSize = 11.sp)
                    }
                }
            }

            Row(
                Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()).padding(horizontal = 12.dp),
                horizontalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                NetFilter.entries.forEach { f ->
                    FilterChip(
                        selected = filter == f,
                        onClick = { filter = f },
                        label = { Text(f.label, fontSize = 11.sp) },
                    )
                }
            }
            Row(
                Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 2.dp),
                horizontalArrangement = Arrangement.spacedBy(6.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text("Sort:", fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                NetSort.entries.forEach { s ->
                    TextButton(onClick = { sort = s }) {
                        Text(s.label, fontSize = 11.sp, fontWeight = if (sort == s) FontWeight.Bold else FontWeight.Normal)
                    }
                }
            }

            if (shown.isEmpty()) {
                Box(Modifier.fillMaxWidth().padding(24.dp), contentAlignment = Alignment.Center) {
                    Text(
                        if (vm.networks.isEmpty())
                            "No sweep yet. Press Scan - results come from the radio, never from this app."
                        else "No network matches this filter.",
                        fontSize = 13.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            } else {
                LazyColumn(Modifier.fillMaxWidth()) {
                    items(shown, key = { it.bssid }) { n ->
                        NetworkRow(n, onClick = { selected = n })
                    }
                    item { Spacer(Modifier.height(24.dp)) }
                }
            }
        }
    }

    selected?.let { n ->
        ModalBottomSheet(onDismissRequest = { selected = null }) { NetworkDetails(n) }
    }
}

@Composable
private fun NetworkRow(n: Network, onClick: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().clickable(onClick = onClick).padding(horizontal = 12.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        QualityBar(n.quality)
        Spacer(Modifier.width(10.dp))
        Column(Modifier.widthIn(min = 0.dp)) {
            Text(
                Contract.displayName(n),
                fontWeight = FontWeight.Medium,
                fontSize = 14.sp,
                color = if (n.hidden) MaterialTheme.colorScheme.onSurfaceVariant else MaterialTheme.colorScheme.onSurface,
            )
            Text(
                "${n.bssid}  ch${n.channel}  ${n.rssi} dBm",
                fontSize = 11.sp,
                fontFamily = FontFamily.Monospace,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Row(horizontalArrangement = Arrangement.spacedBy(4.dp), modifier = Modifier.padding(top = 2.dp)) {
                Tag(n.security, MaterialTheme.colorScheme.primary)
                if (n.wps) Tag("WPS adv.", WeakColor)
                if (n.security == "open") Tag("open", ViolationColor)
                if (n.isStrong) Tag(Contract.strongLabel(), StrongColor)
            }
        }
    }
}

@Composable
private fun Tag(text: String, color: Color) {
    Box(
        Modifier
            .background(color.copy(alpha = 0.15f), RoundedCornerShape(4.dp))
            .padding(horizontal = 5.dp, vertical = 1.dp),
    ) {
        Text(text, fontSize = 9.sp, color = color, fontWeight = FontWeight.Medium)
    }
}

@Composable
private fun QualityBar(quality: Int) {
    Box(
        Modifier
            .background(MaterialTheme.colorScheme.surfaceVariant, RoundedCornerShape(3.dp))
            .width(6.dp)
            .height(38.dp),
    ) {
        Box(
            Modifier
                .fillMaxWidth()
                .height((38 * (quality.coerceIn(0, 100) / 100f)).dp)
                .background(
                    when {
                        quality >= 60 -> StrongColor
                        quality >= 35 -> WeakColor
                        else -> ViolationColor
                    },
                    RoundedCornerShape(3.dp),
                ),
        )
    }
}

@Composable
private fun NetworkDetails(n: Network) {
    Column(Modifier.fillMaxWidth().padding(20.dp)) {
        Text(Contract.displayName(n), fontSize = 18.sp, fontWeight = FontWeight.Bold)
        Spacer(Modifier.height(12.dp))
        Field("BSSID", n.bssid)
        Field("Channel", "${n.channel}  (${n.freqMhz} MHz)")
        Field("Signal", "${n.rssi} dBm   quality ${n.quality}/100")
        Field("Security", n.security)
        Field("WPS advertised", if (n.wps) "yes" else "no")
        Field("Hidden", if (n.hidden) "yes - the SSID is not broadcast" else "no")
        Field("Width", "${n.widthMhz} MHz")
        Field("PHY", if (n.phy.isEmpty()) "-" else n.phy.joinToString("/"))
        Field("Tags", if (n.flags.isEmpty()) "-" else n.flags.joinToString(", "))
        n.ssidHex?.let { Field("SSID (hex)", it) }

        Spacer(Modifier.height(12.dp))
        if (n.hidden) {
            Text(
                "This network does not broadcast its name. The device reports the empty string " +
                    "and this app shows a label; it will never invent a name. A hidden SSID is only " +
                    "revealed when a client probes for it, which ordinary scanning does not do.",
                fontSize = 11.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(8.dp))
        }
        if (n.wps) {
            Text(
                "WPS is advertised in the beacon. That does not mean WPS is enabled, and this app " +
                    "does not attempt to use it.",
                fontSize = 11.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(8.dp))
        }
        if (n.isStrong) {
            Text(
                "WPA3 capable means the access point supports WPA3. For wpa2/wpa3 networks a client " +
                    "may still connect with WPA2, so this is not a claim that the connection is WPA3.",
                fontSize = 11.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        Spacer(Modifier.height(20.dp))
    }
}

@Composable
private fun Field(label: String, value: String) {
    Row(Modifier.fillMaxWidth().padding(vertical = 2.dp)) {
        Text(label, fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.width(120.dp))
        Text(value, fontSize = 12.sp, fontFamily = FontFamily.Monospace)
    }
}
