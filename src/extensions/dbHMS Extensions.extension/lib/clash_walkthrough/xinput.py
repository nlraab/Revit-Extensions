# -*- coding: utf-8 -*-
"""Win32 XInput bindings for Xbox controller polling.

We P/Invoke directly into xinput1_4.dll using .NET's DllImport via
clr / System.Runtime.InteropServices. No Python-side dependency.

Polling model: caller calls poll(0..3) on a timer (~16 ms cadence for
~60 Hz). Returns a GamepadState dict or None if the controller is
disconnected.

References:
    - https://learn.microsoft.com/en-us/windows/win32/xinput/programming-guide
"""


# Standard XInput button bitmasks
XINPUT_BUTTON_A             = 0x1000
XINPUT_BUTTON_B             = 0x2000
XINPUT_BUTTON_X             = 0x4000
XINPUT_BUTTON_Y             = 0x8000
XINPUT_BUTTON_DPAD_UP       = 0x0001
XINPUT_BUTTON_DPAD_DOWN     = 0x0002
XINPUT_BUTTON_DPAD_LEFT     = 0x0004
XINPUT_BUTTON_DPAD_RIGHT    = 0x0008
XINPUT_BUTTON_START         = 0x0010
XINPUT_BUTTON_BACK          = 0x0020
XINPUT_BUTTON_LEFT_THUMB    = 0x0040
XINPUT_BUTTON_RIGHT_THUMB   = 0x0080
XINPUT_BUTTON_LEFT_SHOULDER = 0x0100
XINPUT_BUTTON_RIGHT_SHOULDER= 0x0200

# Stick deadzones recommended by Microsoft
LEFT_THUMB_DEADZONE  = 7849
RIGHT_THUMB_DEADZONE = 8689
TRIGGER_THRESHOLD    = 30


def poll(controller_index=0):
    """Poll controller `controller_index` (0..3).

    Returns a dict like:
        {
            "connected": True,
            "buttons": <int bitmask>,
            "left_trigger": 0..255,
            "right_trigger": 0..255,
            "left_stick": (-1.0..1.0, -1.0..1.0),    # (x, y), deadzone-applied
            "right_stick": (-1.0..1.0, -1.0..1.0),
        }
    or {"connected": False} if no controller is plugged in to that slot.
    """
    raise NotImplementedError


def is_button_pressed(state, button_mask):
    """Convenience: True if `button_mask` is set in state['buttons']."""
    raise NotImplementedError
