# Wiring the DRS-240-24 to the Waveshare USB-to-RS485 adapter

Vendor documents, since neither is redistributed in this repo:
[DRS-240 datasheet](https://www.meanwell.com/Upload/PDF/DRS-240/DRS-240-spec.pdf) ·
[DRS Series instruction manual](https://www.meanwell.com/Upload/PDF/DRS-240,480.pdf) ·
[Waveshare USB TO RS232/485 (C) wiki](https://www.waveshare.com/wiki/USB_TO_RS232/485_(C))

## First: you do not need the cable in that photo

The cable in the MEAN WELL drawing (RJ45 on one end, two 10-pin HRS DF11-10DS
connectors on the other, labelled SBP#2) is the programming cable for the
**SBP-001 smart programmer**, and the SBP-001 only works with the CANBus
variants — the datasheet says so twice: *"The SBP-001 only supports CANBus
version (DRS-240-xxCAN)"*.

Your unit is a **DRS-240-24**. In MEAN WELL's model encoding, a blank suffix
means **built-in MODBus**; `CAN` means CANBus. So you have the Modbus model,
you are not using the SBP-001, and the DF11 end of that cable is irrelevant to
you.

What you actually need is three wires from the RJ45 to the adapter's screw
terminals.

## The DRS-240 RJ45 pinout

From the datasheet, *Terminal Pin No. Assignment (RJ45)*:

| RJ45 pin | Function | Notes |
|---|---|---|
| 1–5 | NC | "Retain for future use" — leave unconnected |
| **6** | **D− / DB** | RS-485 inverting line (Modbus B / D0) |
| **7** | **D+ / DA** | RS-485 non-inverting line (Modbus A / D1) |
| **8** | **GND-AUX** | Signal ground, **isolated** from the +V/−V output terminals |

Pin 1 is the one nearest the latch-side edge as drawn in the datasheet; on the
socket the numbering runs 1 at the top to 8 at the bottom.

## The Waveshare USB TO RS232/485 (C)

It presents a 4-pin cable with a female crimp connector, labelled:

| Waveshare | Meaning |
|---|---|
| `5V` | 5 V out — **do not connect** |
| `GND` | Signal ground |
| `A+/TX` | RS-485 A+ (in RS-485 mode) |
| `B-/RX` | RS-485 B− (in RS-485 mode) |

Set the mode toggle switch to **RS485**. Enable the **120R** termination
switch — you are one end of the bus (see below).

## The connection

| DRS-240 RJ45 | Signal | Waveshare terminal |
|---|---|---|
| pin 7 | D+ / DA | `A+/TX` |
| pin 6 | D− / DB | `B-/RX` |
| pin 8 | GND-AUX | `GND` |
| — | — | `5V` — leave disconnected |

The manual states this directly: *"Connect the DATA+ / DATA− pins of the master
to the corresponding DA (PIN 7) and DB (PIN 6) pins of the RJ45 connector on
the supply. It is recommended to establish a common ground for the
communication system to increase its communication reliability by using
GND_AUX (PIN 8)."*

### Making the cable from a standard patch lead

Cut one end off an ordinary **T568B** Ethernet patch cable and you get the
right conductors on the right pins for free — this is exactly what MEAN WELL's
own cable does, and the datasheet lists the same colours:

| RJ45 pin | T568B colour | Goes to |
|---|---|---|
| 6 | **Green** | `B-/RX` |
| 7 | **White/Brown** | `A+/TX` |
| 8 | **Brown** | `GND` |

Cut back and insulate the other five conductors; do not leave them shorted
together.

> Check the colours on your particular cable rather than trusting the table
> blindly — T568A swaps the green and orange pairs, which moves pin 6 to
> orange. Pins 7 and 8 (the brown pair) are the same in both standards.

### If it does not talk

If `drs240 probe` finds nothing, **swap A+ and B− first**. RS-485 polarity
labelling is inconsistent across the industry and a reversed pair produces
exactly this symptom — silence, no errors. It cannot damage anything.

## More than one DRS-240 on the bus

RS485 is a multi-drop bus, and the bridge polls up to four supplies through a
single adapter. Wire them as a daisy chain, not a star:

```
  Waveshare             DRS-240 "a"           DRS-240 "b"
  120R switch ON        no terminator         120 Ω across pins 6-7
  ┌──────────┐         ┌───────────┐         ┌───────────┐
  │ A+  ──────┼────────┼─ pin 7 ───┼────────┼─ pin 7     │
  │ B-  ──────┼────────┼─ pin 6 ───┼────────┼─ pin 6     │
  │ GND ──────┼────────┼─ pin 8 ───┼────────┼─ pin 8     │
  └──────────┘         └───────────┘         └───────────┘
    one bus end             middle              far bus end
```

Every supply is a tap on the same pair. With RJ45 plugs the practical way to
do that is a punch-down block or an inline coupler per supply, with a short
stub to each one — keep the stubs under about 20 cm, because a long stub is a
reflection the terminators cannot absorb.

Each supply also needs **its own address**, strapped on CN1 (see below). Two
left at the factory `0x83` answer the same request simultaneously, and the
collision surfaces as CRC errors rather than anything that says "duplicate
address".

## Termination

At 115200 baud this is a short bus, so termination matters more than people
expect. The manual recommends 120 Ω at **both** ends:

- **Adapter end:** flip the 120R switch on the Waveshare to ON.
- **Far end:** the DRS-240 has no internal terminator. For a single supply a
  metre or two away you will usually be fine without one. For anything longer,
  for a chain of several supplies, or if you see `drsPollErrors` climbing, fit
  a 120 Ω resistor across RJ45 pins 6 and 7 of the **last** supply in the
  chain — easiest inside the plug shell, or in an inline coupler. Terminate
  the two ends only; a terminator on a middle unit just loads the bus.

Keep the cable away from the AC input and the battery leads, and do not run it
in the same bundle as anything switching hard.

## Serial parameters

Fixed by the DRS-240 and not adjustable:

| | |
|---|---|
| Protocol | Modbus RTU |
| Baud rate | 115200 |
| Data bits | 8 |
| Parity | None |
| Stop bits | 1 |
| Flow control | None |
| Min. request period | 50 ms |
| Max. response time | 12.5 ms |

## Slave address

The Modbus slave ID is `0x8X`, where `X` is a two-bit address strapped on
**CN1** (the 8-pin "force button" header, part JS-2008R-4*2-T):

| CN1 pin | Signal |
|---|---|
| 1 | 3.3 V |
| 2 | GND |
| 3 | RTH+ (battery temperature sensor) |
| 4 | RTH− |
| **5** | **A0** |
| **6** | **A1** |
| 7, 8 | Open: normal. Shorted: force start |

Each address line reads **1 when open** and **0 when shorted to GND**:

| A1 | A0 | Device | Slave ID |
|---|---|---|---|
| 0 | 0 | 0 | `0x80` |
| 0 | 1 | 1 | `0x81` |
| 1 | 0 | 2 | `0x82` |
| 1 | 1 | 3 | **`0x83`** |

With CN1 unconnected — how the unit ships — both lines float high, so the
address is device 3 and **the slave ID is `0x83` (131 decimal)**. That is the
default for a single supply, and it is the ID used in every worked example in
MEAN WELL's manual. `drs240 probe` scans all four regardless.

**With more than one supply on the bus, every one after the first has to be
strapped.** Two bits means four addresses, so four DRS-240s is the hard limit
and there is no `0x84`. A workable convention is to leave the first unit as it
ships and work downwards:

| Supply | Strap | Slave ID | Unit file |
|---|---|---|---|
| first | nothing — as shipped | `0x83` | `units/a.conf` |
| second | A0 (pin 5) to GND | `0x82` | `units/b.conf` |
| third | A1 (pin 6) to GND | `0x81` | `units/c.conf` |
| fourth | A0 and A1 to GND | `0x80` | `units/d.conf` |

Put each supply's address in its own unit file as `[modbus] slave_id`, then
confirm the bus two ways: `drs240 probe` should list one line per supply, and
`drs240 units` prints the addresses the bridge will actually use.

Strapping is the only way to change the address: it is a hardware input, not
a register, so there is nothing `drs240 set` can do about it. Do it with the
supply unplugged.

A0 and A1 are inputs with pull-ups, so "strapping" means a wire from that pin
to CN1 pin 2 (GND) — a jumper, a DIP switch, or a link in the header shell.
Nothing else on CN1 needs touching.

## Safety notes

- GND-AUX on pin 8 is isolated from the DC output terminals. Do not bond it to
  −V "to be safe" — that defeats the isolation the design gives you.
- The RJ45 socket is on the low-voltage face of the unit, but the terminal
  blocks a few centimetres away carry mains. Make the cable up with the supply
  **unplugged**.
- Nothing here needs the CN1 header. Leave it alone unless you are strapping
  addresses or fitting the battery NTC.
