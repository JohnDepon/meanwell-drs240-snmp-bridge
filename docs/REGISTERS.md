# DRS-240 Modbus register reference

Everything here is read from the MEAN WELL
[DRS Series instruction manual](https://www.meanwell.com/Upload/PDF/DRS-240,480.pdf) section 5.4 and the
[DRS-240 datasheet](https://www.meanwell.com/Upload/PDF/DRS-240/DRS-240-spec.pdf). Neither is redistributed in this repo; check the
revision date on the vendor's copy against what you read here.

Transcribed from the MEAN WELL *DRS Series Instruction Manual* (`DRS-240,480-E`),
section 5.4.1.6. This is what `drs240/registers.py` implements.

## Link settings

| | |
|---|---|
| Protocol | Modbus RTU, master/slave |
| Baud rate | 115200 |
| Framing | 8 data bits, 1 stop bit, no parity, no flow control |
| Byte order | Big-endian (high byte first), standard Modbus |
| Error check | CRC-16, transmitted low byte first |
| Slave ID | `0x8X`, X = 0–3 strapped on CN1 A0/A1; `0x83` unstrapped. Four supplies per bus maximum |
| Broadcast | `0x00` — writes only, never reads |
| Min. request period | 50 ms (controller → PSU) |
| Max. response time | 12.5 ms (PSU → controller) |

## Function codes

| Code | Name | Used for |
|---|---|---|
| `0x03` | Read Holding Registers | Parameters, settings, status words, identification |
| `0x04` | Read Input Registers | Live analogue readings |
| `0x06` | Preset Single Register | Writing a parameter |

Note that the live measurements are **input** registers (`0x04`) while the
status words are **holding** registers (`0x03`). Reading a register with the
wrong function code returns exception `0x02`, illegal data address.

## Registers

| Address | Regs | Name | FC | Scaling | Meaning |
|---|---|---|---|---|---|
| `0x0000` | 1 | OPERATION | 03/06 | — | 0 = output off, 1 = output on |
| `0x0020` | 1 | VOUT_SET | 03/06 | ×0.01 V | Output voltage set point |
| `0x0040` | 1 | FAULT_STATUS | 03 | bits | Protection and fault flags |
| `0x0050` | 1 | READ_VIN | 04 | ×0.1 V | AC input voltage |
| `0x0060` | 1 | READ_VOUT | 04 | ×0.01 V | DC output voltage |
| `0x0061` | 1 | READ_IOUT | 04 | ×0.01 A | DC output current |
| `0x0062` | 1 | READ_TEMPERATURE_1 | 04 | ×0.1 °C | Internal ambient, signed |
| `0x0080`–`0x0085` | 6 | MFR_ID | 03 | ASCII | `MEANWELL` |
| `0x0086`–`0x008B` | 6 | MFR_MODEL | 03 | ASCII | `DRS-240-24` |
| `0x008C`–`0x008E` | 3 | MFR_REVISION | 03 | binary | Firmware version |
| `0x008F`–`0x0090` | 2 | MFR_LOCATION | 03 | ASCII | `TWN` or `CHN` |
| `0x0091`–`0x0093` | 3 | MFR_DATE | 03 | ASCII | Manufacturing date |
| `0x0094`–`0x0099` | 6 | MFR_SERIAL | 03 | ASCII | Serial number |
| `0x00B0` | 1 | CURVE_CC | 03/06 | ×0.01 A | Charge constant current |
| `0x00B1` | 1 | CURVE_CV | 03/06 | ×0.01 V | Charge constant voltage |
| `0x00B2` | 1 | CURVE_FV | 03/06 | ×0.01 V | Charge float voltage |
| `0x00B3` | 1 | CURVE_TC | 03/06 | ×0.01 A | Charge taper current |
| `0x00B4` | 1 | CURVE_CONFIG | 03/06 | bits | Curve selection and options |
| `0x00B5` | 1 | CURVE_CC_TIMEOUT | 03/06 | min | Constant-current stage limit |
| `0x00B6` | 1 | CURVE_CV_TIMEOUT | 03/06 | min | Constant-voltage stage limit |
| `0x00B7` | 1 | CURVE_FV_TIMEOUT | 03/06 | min | Float stage limit |
| `0x00B8` | 1 | CHG_STATUS | 03 | bits | Charger stage and battery flags |
| `0x00C0`–`0x00C2` | 3 | SCALING_FACTOR | 03 | nibbles | The unit's own scaling ratios |
| `0x00C3` | 1 | SYSTEM_STATUS | 03 | bits | System-level status |
| `0x00C4` | 1 | SYSTEM_CONFIG | 03/06 | bits | EEPROM and power-on behaviour |
| `0x00D0` | 1 | BAT_UVP_SET | 03/06 | ×0.01 V | Battery low cut-off |
| `0x00D1` | 1 | Force_BAT_UVP_SET | 03/06 | ×0.01 V | Forced battery low cut-off |
| `0x00D2` | 1 | UPS_CONFIG | 03/06 | bits | UPS feature enables |
| `0x00D3` | 1 | READ_VBAT | 04 | ×0.01 V | Battery voltage |
| `0x00D4` | 1 | READ_IBAT | 04 | ×0.01 A | Battery current, signed |
| `0x00D5` | 1 | READ_BAT_TEMPERATURE | 04 | ×0.1 °C | Battery NTC, signed |
| `0x00E0` | 1 | AC_Fail_LL_SET | 03/06 | ×0.1 V | AC fail low threshold |
| `0x00E1` | 1 | AC_Fail_HL_SET | 03/06 | ×0.1 V | AC fail high threshold |
| `0x00E2` | 1 | AC_OK_LL_SET | 03/06 | ×0.1 V | AC OK low threshold |
| `0x00E3` | 1 | AC_OK_HL_SET | 03/06 | ×0.1 V | AC OK high threshold |
| `0x00E4` | 1 | TIME_BUFFERING | 03/06 | min | Buffering time (T1) |
| `0x00E8` | 1 | UPS_Delay_Time | 03/06 | s | Shutdown process time (T2) |
| `0x00E9` | 1 | UPS_Shutdown_Time | 03/06 | s | Restart delay (T3) |

`TIME_BUFFERING`, `UPS_Delay_Time` and `UPS_Shutdown_Time` only take effect
when the corresponding enable bit in `UPS_CONFIG` low byte is set — bit 2,
bit 4 and bit 5 respectively.

The manual does not state a factor for the four AC threshold registers
(`0x00E0`–`0x00E3`). ×0.1 V above is inferred from them being AC-side
quantities, which is what the VIN factor covers; treat it as unconfirmed
until you compare a read-back against the value you set. The bridge does not
read or scale these registers, so nothing it publishes depends on the guess.

### A caution about these addresses

The manual contains **two conflicting numbering schemes**. Its CANBus command
list (section 5.4.4) gives each multi-byte block one command code
(`MFR_ID_B0B5` = `0x0080`, `MFR_ID_B6B11` = `0x0081`, `SCALING_FACTOR` =
`0x00C0`, `SYSTEM_STATUS` = `0x00C1`); the Modbus table in 5.4.1.6 gives real
16-bit register addresses, where the same blocks span several registers.

The worked example in 5.4.2.1 settles it for Modbus: reading **6 registers**
from `0x0080` returns the **12 ASCII bytes** `MEANWELL    `. That is only
consistent with the register-per-16-bit scheme, which is what the table above
and the code use. The CAN command codes are not Modbus addresses.

If your firmware disagrees, `drs240 probe --dump` reads every address above
with both function codes and shows you what actually answers.

## Scaling

> Actual value = communication reading value × Factor

Factors are not fixed constants — the unit reports its own in
`SCALING_FACTOR`, one nibble per quantity:

| Nibble | Factor | | Nibble | Factor |
|---|---|---|---|---|
| `0x0` | command not supported | | `0x7` | 1.0 |
| `0x4` | 0.001 | | `0x8` | 10 |
| `0x5` | 0.01 | | `0x9` | 100 |
| `0x6` | 0.1 | | `0xA`–`0xF` | reserved |

Nibble layout across the 6 bytes, byte 0 being the first on the wire:

| Byte | Bits 4–7 | Bits 0–3 |
|---|---|---|
| 0 | IOUT | VOUT |
| 1 | FAN_SPEED | VIN |
| 2 | CURVE_TIMEOUT | TEMPERATURE_1 |
| 3 | reserved | IIN |
| 4–5 | reserved | reserved |

For the DRS series the manual's command list documents VOUT = 0.01,
IOUT = 0.01, VIN = 0.1, TEMPERATURE = 0.1; the bridge falls back to these if
`SCALING_FACTOR` is unreadable. The battery registers have no factors of their
own and share the output voltage, output current and temperature factors.

## Status bits

### FAULT_STATUS (`0x0040`)

High byte reserved. Low byte:

| Bit | Name | 1 means |
|---|---|---|
| 0 | FAN_FAIL | Fan abnormal — *not supported on the DRS-240* |
| 1 | OTP | Over-temperature protection |
| 2 | OVP | Output over-voltage protection |
| 3 | OLP | Output over-current protection |
| 4 | SHORT | Short-circuit protection |
| 5 | AC_FAIL | AC input out of range |
| 6 | OP_OFF | DC output is off |
| 7 | HI_TEMP | Ambient temperature abnormal |

### CHG_STATUS (`0x00B8`)

Low byte:

| Bit | Name | 1 means |
|---|---|---|
| 0 | FULLM | Fully charged |
| 1 | CCM | Constant-current mode |
| 2 | CVM | Constant-voltage mode |
| 3 | FVM | Float mode |
| 4–6 | — | reserved |
| 7 | DCM | Discharging (0 = charging) |

High byte:

| Bit | Name | 1 means |
|---|---|---|
| 0–1 | — | reserved |
| 2 | NTCER | Temperature-compensation circuit shorted |
| 3 | BTNC | **No** battery detected |
| 4 | BUFFTOF | Buffering timed out |
| 5 | CCTOF | Constant-current stage timed out |
| 6 | CVTOF | Constant-voltage stage timed out |
| 7 | FVTOF | Float stage timed out |

Note BTNC is inverted relative to how you would want to graph it; the bridge
publishes `drsBatteryPresent` as its complement so that 1 is healthy.

### SYSTEM_STATUS (`0x00C3`)

High byte reserved. Low byte:

| Bit | Name | 1 means |
|---|---|---|
| 0 | M/S | Parallel master — *not supported* |
| 1 | DC_OK | Secondary DC output voltage normal |
| 2 | PFC_OK | Primary PFC normal — *not supported* |
| 3 | ORING_OFF | ORING MOS forced off — *not supported* |
| 4 | ADL_ON | Active dummy load on — *not supported* |
| 5 | INITIAL_STATE | Initialisation finished |
| 6 | EEPER | EEPROM data access error |
| 7 | CHG/UPS | UPS mode (0 = charging mode) |

Bits marked *not supported* read 0 on the DRS-240 and should be ignored.

### OP_OFF is not a fault during an outage

Observed on a real DRS-240-24: **every** mains failure sets `FAULT_STATUS`
bit 6 (`OP_OFF`) alongside bit 5 (`AC_FAIL`), giving `fault = 0x0060`. The
output terminals stay live throughout — 27 V measured with the bit set, fed
from the battery through the ORing path — and `SYSTEM_STATUS.DC_OK` stays 1.

The bit reports the state of the unit's own AC/DC converter output stage, not
whether the load has power. Taking it at face value produces a spurious
"DC output switched off" alongside every outage, and in RFC 1628 terms reports
`upsOutputSource = none(2)` for something that is plainly `battery(5)`.

`registers.is_output_really_off()` is the single place that decides this: the
bit only counts when the unit is not running on battery. The event detector,
the UPS-MIB view and the health roll-up all defer to it.

Observed on the same unit: `INITIAL_STATE` also
reads 0 on a unit that has been floating for hours, which by the manual's
wording would mean "still initialising". The firmware appears simply not to
set it. Treat `drsInitialised` as uninformative on this model; nothing in the
bridge's health roll-up depends on it.

### SYSTEM_CONFIG (`0x00C4`)

High byte:

| Bits | Name | Values |
|---|---|---|
| 0–1 | EEP_CONFIG | `00` write to EEPROM immediately (default), `01` after 1 min unchanged, `10` after 10 min unchanged |
| 2 | EEP_OFF | 0 save parameters to EEPROM (default), 1 do not save |

Low byte:

| Bits | Name | Values |
|---|---|---|
| 0 | MOD_CTRL | Modbus control source — *not supported* |
| 1–2 | OPERATION_INIT | Power-on output state: `00` off, `01` on, `10` last value |

**This is why `drs240 set` warns you.** With the factory default of
`EEP_CONFIG = 00`, every parameter write hits the EEPROM straight away.
Repeatedly writing a set point on a timer will wear it out.

## Worked examples from the manual

Read the manufacturer name from slave 3 (`MFR_ID`, 6 registers from `0x0080`):

```
request   83 03 00 80 00 06 DA 02
response  83 03 0C 4D 45 41 4E 57 45 4C 4C 20 20 20 20 4A 8C
                  M  E  A  N  W  E  L  L
```

Read the output voltage (`READ_VOUT`, input register `0x0060`):

```
request   83 04 00 60 00 01 2F F6
response  83 04 02 15 7C CE 5F      0x157C = 5500, ×0.01 = 55.00 V
```

Turn the output on (`OPERATION` = 1):

```
request   83 06 00 00 00 01 56 28
response  echo of the request
```

Set the output to 56.00 V (`VOUT_SET`, 5600 = `0x15E0` at ×0.01):

```
request   83 06 00 20 15 E0 99 3A
response  echo of the request
```

Every CRC above was verified against this implementation.

## The Form-C relays: the only push the DRS-240 has

Modbus RTU is strictly master/slave — a slave never speaks unless polled, and
RJ45 pins 1–5 are "retain for future use", so there is no interrupt line on
the serial side. The unit does, however, carry four dry-contact Form-C relays
on **TB4**, and those change state in hardware:

| TB4 pins | Signal | Closed / open |
|---|---|---|
| 1, 2, 3 | AC fail | ON = AC OK, OFF = AC fail |
| 4, 5, 6 | DC OK | ON = DC OK, OFF = DC fail (output < 90 % rated) |
| 7, 8, 9 | Battery low / abnormal / disconnected | ON = battery OK |
| 10, 11, 12 | Charger fail | ON = charger OK |

Rated 30 Vdc / 1 A maximum, and they are **dry contacts** — the datasheet is
explicit that "an external voltage source is required for this function".
The middle pin of each group is the common; the datasheet's Table 1.1 gives
the closed/open combinations per state.

The DC-UPS specification says the unit transfers to battery "within 10 ms of
AC failure", and the relay follows the same event, so an AC-fail contact wired
to a GPIO with an internal pull-up gives genuinely interrupt-driven detection
— microseconds rather than the poller's couple of seconds. The current in that
loop is microamps, far inside the contact rating.

The bridge does not use these today; it detects everything over Modbus.
