package com.hseen.wifianalyzer.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

private val SignalBlue = Color(0xFF2F6FED)
private val WarnAmber = Color(0xFFB26A00)
private val AlertRed = Color(0xFFB3261E)
private val OkGreen = Color(0xFF1B7F3B)

val WeakColor = WarnAmber
val ViolationColor = AlertRed
val StrongColor = OkGreen

private val Light = lightColorScheme(
    primary = SignalBlue,
    secondary = Color(0xFF4A5568),
)

private val Dark = darkColorScheme(
    primary = Color(0xFF8AB4FF),
    secondary = Color(0xFFA0AEC0),
)

@Composable
fun WifiAnalyzerTheme(darkTheme: Boolean = isSystemInDarkTheme(), content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = if (darkTheme) Dark else Light, content = content)
}
