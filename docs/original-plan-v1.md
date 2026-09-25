Confidence Score: 98%  
DIRECT ANSWER:  
Below is the complete, self-contained **F1 26 Pit Wall Crew System Master Strategy Plan** formatted in clean Markdown. You can copy or save the text block below as MASTER\_PLAN.md. It consolidates all our discussions, including 2026 UDP specifications, zero-GC memory compaction, dual-mode strategy logic, audio priority queuing, and high-scan mobile/secondary UI layout.  
STRUCTURED DETAILS:

# **MASTER\_PLAN.md**

Markdown  
\# F1 26 Pit Wall Crew System — Technical Master Plan

\#\# 1\. Executive Summary & Architecture Overview

The **\*\*F1 26 Pit Wall Crew System\*\*** is a real-time, low-latency race engineering server and dashboard designed to process F1 26 game telemetry (port 20777 @ 60Hz) and deliver high-value, actionable audio and visual coaching.

\#\#\# High-Level System Architecture

┌─────────────────────────────────────────────────────────────────────────┐  
│ F1 26 GAME ENGINE (PC / UDP) │  
│ Stream: Port 20777 @ 60Hz (Format 2026\) │  
└────────────────────────────────────┬────────────────────────────────────┘  
│  
▼  
┌─────────────────────────────────────────────────────────────────────────┐  
│ BACKEND ENGINE (FASTAPI / PYTHON 3.11+) │  
│ │  
│ ┌─────────────────────────────┐ ┌─────────────────────────────┐ │  
│ │ Async Socket Listener │ ───\> │ Struct Unpacker & Enums │ │  
│ │ (Heartbeat Pause Detection) │ │ (F1 26 Spec / 24-Car Array) │ │  
│ └─────────────────────────────┘ └──────────────┬──────────────┘ │  
│ │ │  
│ ▼ │  
│ ┌─────────────────────────────┐ ┌─────────────────────────────┐ │  
│ │ Dual-EMA Thermal Filter │ │ Zero-GC Memory Compactor │ │  
│ │ (3s Fast / 30s Slow) │ │ (3s Frame Buffer \+ 5-Lap) │ │  
│ └──────────────┬──────────────┘ └──────────────┬──────────────┘ │  
│ │ │ │  
│ └──────────────────┬──────────────────┘ │  
│ ▼ │  
│ ┌───────────────────────────────┐ │  
│ │ Session-Aware Strategy Engine│ │  
│ │ (Quali vs Race / 25% Sprint) │ │  
│ └───────────────┬───────────────┘ │  
│ │ │  
│ ▼ │  
│ ┌───────────────────────────────┐ │  
│ │ Audio Priority Preemption Queue│ │  
│ │ (TTL: 1500ms, Message Merger) │ │  
│ └───────────────┬───────────────┘ │  
└────────────────────────────────────┼────────────────────────────────────┘  
│ WebSockets (Local IP : 8000\)  
▼  
┌─────────────────────────────────────────────────────────────────────────┐  
│ SECONDARY DISPLAY / MOBILE PHONE (FRONTEND) │  
│ Chrome / Android WebView @ http://\<PC\_IP\>:8000 │  
│ \- Static High-Scan Text Dashboard (HTML5 / Tailwind) │  
│ \- Web Speech API Native Text-to-Speech Engine │  
└─────────────────────────────────────────────────────────────────────────┘

\---

\#\# 2\. Ingestion Layer & F1 26 UDP Specification

\#\#\# Required UDP Packets & Config Parameters  
\* \*\*In-Game UDP Settings:\*\* \`UDP Telemetry: On\`, \`Port: 20777\`, \`Send Rate: 60Hz\`, \`Format: 2026\`, \`Broadcast: Off\` (or On for console).  
\* \*\*Grid Capacity:\*\* 24 Car Array Slots (\`0..23\`).

| Packet Name | ID | Frequency | Target Attributes | Tactical Purpose |  
| :--- | :--- | :--- | :--- | :--- |  
| \*\*Motion\*\* | 0 | 60Hz | \`mgForceLateral\`, \`mgForceLongitudinal\` (\`int16 / 1000.0f\`) | Mechanical tyre loading calculations. |  
| \*\*Session\*\* | 1 | 1Hz | \`m\_trackLength\`, \`m\_weather\`, \`m\_weatherForecastSamples\[56\]\`, \`m\_safetyCarStatus\`, \`m\_raceDistance\`, \`m\_sessionType\`, \`m\_trackId\` | Session mode toggle, weather crossovers, safety car flags, and pit loss scaling. |  
| \*\*Lap Data\*\* | 2 | 20Hz | \`m\_lapDistance\`, \`m\_carPosition\`, \`m\_deltaToCarInFrontInMS\`, \`m\_pitStatus\`, \`m\_sector1TimeInMS\`, \`m\_sector2TimeInMS\` | Sector deltas, positional gaps, traffic slots, and clean air exit windows. |  
| \*\*Car Setups\*\* | 5 | 2Hz | \`m\_frontWing\`, \`m\_rearWing\` | Tracking active aero setup and pre-pit stop front wing adjustments. |  
| \*\*Car Telemetry\*\*| 6 | 60Hz | \`m\_tyresSurfaceTemperature\[4\]\`, \`m\_tyresInnerTemperature\[4\]\`, \`m\_brakeBias\` | Dual-layer surface/core thermals and in-car brake bias monitoring. |  
| \*\*Car Status\*\* | 7 | 2Hz | \`m\_tyresWear\[4\]\`, \`m\_visualTyreCompound\`, \`m\_tyresAgeLaps\`, \`m\_ersStoreEnergy\` (Joules), \`m\_differentialOnThrottle\`, \`m\_differentialOffThrottle\` | Wear cliff prediction, 4.0 MJ ERS store tracking, and differential setup tuning. |  
| \*\*Car Damage\*\* | 10| 2Hz | \`m\_frontWingLeftWingDamage\`, \`m\_floorDamage\`, \`m\_engineDamage\` | Downforce loss and engine component wear checks. |  
| \*\*Telemetry 2\*\* | 16| 60Hz | \`m\_activeAeroMode\` (0 \= High Downforce, 1 \= Low Drag), MGU-K Harvest Rate | Active aero state verification and battery harvesting optimization. |

\#\#\# Pause & Flashback Detection Protocol  
To prevent stale telemetry or clock drift during single-player pauses or flashbacks:  
\$\$\\text{IF } \\text{frame.m\\\_sessionTime} \\le \\text{previous\\\_frame.m\\\_sessionTime} \\implies \\text{PAUSE / FLASHBACK DETECTED}\$\$  
\* \*\*Action:\*\* Immediately flush the 3-second Fast EMA buffer, freeze strategy timers, and ignore frame deltas until \`m\_sessionTime\` resumes advancing.

\---

\#\# 3\. Zero-GC Memory Compactor & Thermal Filtering

To prevent Python Garbage Collection (GC) pauses from causing micro-stutters in-game, raw 60Hz frame objects are discarded immediately after processing.

\#\#\# Dual Ring Buffer Architecture  
1\. \*\*Fast Frame Sliding Window (\`collections.deque(maxlen=180)\`):\*\*  
   \* Retains only the last 3 seconds of raw telemetry (\~230 KB). Automatically evicts old frames in \$O(1)\$ constant time.  
   \* Feeds the \*\*Fast Thermal EMA (3-Second Window)\*\* for short-term driver alerts:  
     \$\$\\text{EMA}\_{\\text{fast}}(t) \= (\\text{Temp}\_{\\text{surface}} \\times \\alpha\_{\\text{fast}}) \+ (\\text{EMA}\_{\\text{fast}}(t-1) \\times (1 \- \\alpha\_{\\text{fast}}))\$\$  
2\. \*\*On-The-Fly Lap Accumulator:\*\*  
   \* Accumulates scalar primitives (\`running\_wear\_sum\`, \`max\_surface\_temp\`, \`fuel\_at\_start\`) during an active lap without allocating frame objects.  
3\. \*\*Lap Summary Condenser (\`LapSummary\` Dataclass):\*\*  
   \* At the start/finish line, condenses \~4,800 raw frames into a single \~200-byte struct:  
     \`\`\`python  
     @dataclass  
     class LapSummary:  
         lap\_number: int  
         lap\_time\_ms: int  
         fuel\_consumed: float  
         wear\_delta\_axle: float  
         max\_core\_temp: float  
         valid\_lap: bool  \# False if Lap 1, In-Lap, Out-Lap, SC/VSC, or Weather Transition  
         lap\_type: str    \# "FLYING", "IN\_LAP", "OUT\_LAP", "SAFETY\_CAR"  
     \`\`\`  
4\. \*\*Rolling Strategy History Buffer (\`collections.deque(maxlen=5)\`):\*\*  
   \* Stores the last 5 \`LapSummary\` structs (\~1 KB total footprint). Guarantees total backend memory remains under 1 MB.

\---

\#\# 4\. Session-Aware Strategy Engines

\#\#\# Qualifying Mode Engine (\`m\_sessionType \== 5..9\`)  
\* \*\*Out-Lap Thermal Priming (Prep Lap):\*\*  
  \* Target Windows: Softs Surface/Core at \*\*85°C–95°C\*\*; Brakes at \*\*350°C–500°C\*\*.  
  \* Cues: \*"Front Left cold (74°C). Drag brakes through Sector 3"\* or \*"Surface hot. Stop weaving."\*  
\* \*\*Clean Air Slot Finder:\*\*  
  \* Searches 24-car array for a \*\*4.0s to 6.0s\*\* gap window before releasing from the pit lane.  
\* \*\*Progressive Sector Delta Abort Advisor:\*\*  
  \* Compares live micro-sector deltas against session personal best (\$PB\_{\\text{sector}}\$):  
    \* \*\*Q1 Threshold:\*\* \$+0.500\\text{s}\$ delta \$\\implies\$ \*"Sector 1 \+0.520s. Abort lap—recharge ERS."\*  
    \* \*\*Q2 Threshold:\*\* \$+0.350\\text{s}\$ delta \$\\implies\$ \*"Sector 1 \+0.370s. Abort lap."\*  
    \* \*\*Q3 Threshold:\*\* \$+0.200\\text{s}\$ delta \$\\implies\$ \*"Sector 1 \+0.210s. Abort lap—save ERS for final run."\*

\#\#\# Race Mode Engine (\`m\_sessionType \== 10..13\`)

\#\#\#\# 1\. Invalid Lap Filtering Rules  
A lap is marked \`valid\_lap \= False\` and excluded from \$P\_{\\text{rolling}}\$ pace calculations if:  
\* \`lap\_number \== 1\` (Standing start & cold tyres).  
\* \`m\_pitStatus \> 0\` at any point during the lap (\`IN\_LAP\`).  
\* Immediately follows an In-Lap (\`OUT\_LAP\`).  
\* \`m\_safetyCarStatus \> 0\` at any point during the lap (\`SAFETY\_CAR\`).  
\* Weather state changes during the lap (\`TRANSITION\_LAP\`).

\#\#\#\# 2\. Dynamic 25% Sprint vs. 100% Distance Scaling  
\* Reads \`m\_raceDistance\` from \`Packet ID 1\` (\`3\` \= 25% Sprint, \`5\` \= 50%, \`7\` \= 100%).  
\* Wear cliff prediction evaluates \*\*Slow Core EMA (30-second window)\*\* and wear %:  
  \$\$\\text{Target Cliff Lap} \= \\text{Current Lap} \+ \\left( \\frac{\\text{Wear Threshold} \- \\text{Max Current Wear \\%}}{W\_{\\text{lap}}} \\right)\$\$  
  \*(Wear Threshold \= 48% for 25% Sprint; 60% for 100% Full Race).\*

\#\#\#\# 3\. In-Car Setup Balancing  
\* \*\*Front Overheat / Understeer:\*\* If \$T\_{\\text{FL/FR Core}} \> 102^\\circ\\text{C}\$ and Front Wear leads Rear by \$\\ge 6\\%\$:  
  \* \*"Shift Brake Bias \-1% rearward. Open Off-Throttle Diff \-5%."\*  
\* \*\*Rear Overheat / Traction Loss:\*\* If \$T\_{\\text{RL/RR Core}} \> 100^\\circ\\text{C}\$ and Rear Wear leads Front by \$\\ge 6\\%\$:  
  \* \*"Shift Brake Bias \+1% forward. Lower On-Throttle Diff \-5%."\*  
\* \*\*Pre-Pit MFD Front Wing Call:\*\* 2 laps before scheduled pit stop:  
  \* \*"Mandatory stop next lap. Set MFD Front Wing: \+1 Click for Stint 2."\*

\#\#\#\# 4\. Rival Filtering & Pit Strategy  
\* \*\*Rival Scope Filter (Max 3 Targets):\*\*  
  1\. \*\*Target Ahead (\$P-1\$):\*\* Tracks distance gap, compound, and pace drop.  
  2\. \*\*Chaser Behind (\$P+1\$):\*\* Evaluated \*\*only\*\* when gap \$\< 5.0\\text{s}\$. Tracks DRS threat and active aero.  
  3\. \*\*Pit Exit Rival:\*\* Position projected backward by track pit loss distance modulo track length:  
     \$\$D\_{\\text{loss}} \= L\_{\\text{track}} \\times \\left( \\frac{T\_{\\text{pit\\\_loss}}}{P\_{\\text{rolling}}} \\right), \\quad D\_{\\text{target}} \= (D\_{\\text{player}} \- D\_{\\text{loss}}) \\pmod{L\_{\\text{track}}}\$\$  
\* \*\*Multiplayer Rival Purge Grace Buffer:\*\* Missing rivals are purged only after \*\*2 consecutive missing sector updates\*\* or explicit retirement status (\`m\_resultStatus \>= 3\`).  
\* \*\*Free Pit Stop Rule:\*\* If \$\\text{Gap to Car Behind} \> T\_{\\text{pit\\\_loss}}\$, trigger: \*"Free Pit Stop available. Zero position loss—BOX THIS LAP."\*  
\* \*\*Neutralisation Pit Loss Savings:\*\* Green Flag (\$\\approx 21\\text{s}\$) vs. VSC (\$\\approx 12\\text{s}\$) vs. Full SC (\$\\approx 10\\text{s}\$). If \$SC \> 0\$ and Tyre Wear \$\\ge 25\\% \\implies\$ \*"VSC deployed. Cheap stop open—BOX THIS LAP."\*

\---

\#\# 5\. Circuit Pit Loss Lookup Table (\`TRACK\_PIT\_LOSS\_SEC\`)

\`\`\`python  
TRACK\_PIT\_LOSS\_SEC \= {  
    0: 21.0,  \# Melbourne  
    1: 20.5,  \# Paul Ricard  
    2: 18.0,  \# Shanghai  
    3: 18.5,  \# Sakhir (Bahrain)  
    4: 19.5,  \# Catalunya  
    5: 22.0,  \# Monaco  
    6: 18.0,  \# Montreal  
    7: 24.5,  \# Silverstone  
    8: 20.0,  \# Hockenheim  
    9: 20.5,  \# Hungaroring  
    10: 21.5, \# Spa-Francorchamps  
    11: 18.0, \# Monza  
    12: 21.0, \# Singapore  
    13: 19.0, \# Suzuka  
    14: 20.0, \# Abu Dhabi  
    15: 19.5, \# Austin (COTA)  
    16: 19.0, \# Interlagos  
    17: 18.5, \# Austria (Red Bull Ring)  
    18: 20.0, \# Sochi  
    19: 19.5, \# Mexico  
    20: 19.0, \# Baku  
    21: 18.5, \# Zandvoort  
    22: 20.0, \# Imola  
    23: 19.5, \# Jeddah  
    24: 19.0, \# Miami  
    25: 20.5, \# Las Vegas  
}

## **6\. Audio Dispatcher & Priority Queue**

### **Priority Levels & Preemption Rules**

* **Priority 1 (CRITICAL):** Safety Car, VSC, Immediate Box Call. *Action: Cancels active speech immediately and plays.*  
* **Priority 2 (TACTICAL):** Thermal Overheat, In-Car Setup Adjustment. *Action: Preempts Priority 3; plays after P1.*  
* **Priority 3 (INFORMATIONAL):** Sector Deltas, Gap Updates, Fuel Status. *Action: Queues behind active messages.*

### **Parallel Message Merging**

If P1 and P2 fire simultaneously, they merge into a single call:

* *"Box this lap for Mediums. Ease off trail-braking—Front Left is at 108 degrees."*

### **Audio TTL Protection**

Every audio WebSocket payload contains ttl: 1500 (1500ms Time-To-Live). If OS browser audio queuing delays a message by \$\> 1.5\\text{s}\$, the frontend automatically drops it to prevent stale calls.

## **7\. Frontend Interface & Mobile Hardware Setup**

### **Hardware Setup**

* **Device:** Spare Android Phone (or Monitor 2\) connected to the same home Wi-Fi network.  
* **URL:** http://\<PC\_LOCAL\_IP\>:8000 opened in mobile Chrome / WebView.  
* **Audio:** Plays directly through phone speaker or connected Bluetooth earbud via Web Speech API.

### **High-Scan Visual UI Layout**

┌─────────────────────────────────────────────────────────────────────────┐  
│ \[LIVE STATUS: CONNECTED 60Hz\]             SESSION: QUALI (Q3) / LAP 2   │  
├───────────────────────────────────┬─────────────────────────────────────┤  
│         TYRE THERMAL STATE        │          TARGET & DELTA STATE       │  
│                                   │                                     │  
│     FL: 106°C  │  FR: 98°C        │  CURRENT DELTA: \+0.220s             │  
│    \[ OVERHEAT \]│  \[ OPTIMAL \]     │  STATUS: ABORT THRESHOLD REACHED    │  
│   ─────────────┼─────────────     │                                     │  
│     RL: 94°C   │  RR: 95°C        │  TARGET AHEAD (P2): \+1.4s           │  
│    \[ OPTIMAL \] │  \[ OPTIMAL \]     │  CLEAN AIR SLOT: OPEN (+5.2s)       │  
├───────────────────────────────────┴─────────────────────────────────────┤  
│                     PRIMARY STRATEGY BANNER TEXT                        │  
│                 \>\>\>  ABORT HOTLAP \- RECHARGE ERS  \<\<\<                   │  
├─────────────────────────────────────────────────────────────────────────┤  
│ ENGINEER RADIO LOG                                                      │  
│ \[Sector 1\] \> "Delta \+0.220s exceeds Q3 threshold (+0.200s). Abort."     │  
│ \[Out-Lap\]  \> "Front Left 76°C. Drag brakes through final sector."       │  
└─────────────────────────────────────────────────────────────────────────┘

* **Font Sizes:** Primary Banners \$\\ge 72\\text{pt}\$ Monospace; Secondary Blocks \$\\ge 36\\text{pt}\$ Monospace.  
* **Color States:** Green \= Optimal, Yellow \= Warning, Red \= Overheat / Abort / Box. Updates instantly on state diff without CSS animations.

## **8\. Directory Map & Step-by-Step Implementation Roadmap**

### **Project File Structure**

f1-pitwall/  
├── backend/  
│   ├── main.py                  \# FastAPI application & WebSocket broadcast server  
│   ├── core/  
│   │   ├── udp\_listener.py      \# Async socket reader (Port 20777\) & heartbeat pause check  
│   │   └── struct\_unpacker.py   \# Binary unpacker for F1 26 (IDs 0, 1, 2, 5, 6, 7, 10, 16\)  
│   ├── engine/  
│   │   ├── compactor.py         \# Zero-GC Dual Ring Buffer & LapSummary condenser  
│   │   ├── thermal\_filter.py    \# Fast (3s) and Slow (30s) EMA smoothing models  
│   │   ├── rival\_filter.py      \# 24-car indexer (Target Ahead, Behind, Pit Exit Rival)  
│   │   ├── audio\_queue.py       \# Priority preemption queue, message merger & TTL filter  
│   │   ├── quali\_engine.py      \# Out-lap thermals, clean air slot, Q1/Q2/Q3 progressive aborts  
│   │   └── race\_engine.py       \# Invalid lap filtering, wear cliff, setup balance & VSC/SC/Weather  
│   └── models/  
│       ├── protocol.py          \# Typed Enums (Compounds, Sessions, Weather, Packets)  
│       └── telemetry\_state.py   \# Shared in-memory telemetry state model  
└── frontend/  
    ├── index.html               \# Monitor 2 / Android Phone high-scan static UI  
    └── app.js                   \# WebSocket client & Web Speech API TTS audio manager

### **Tomorrow's Coding Action Plan**

  STEP 1: Core Telemetry Engine       STEP 2: State & Compactor           STEP 3: Strategy Engines            STEP 4: UI & Audio  
┌──────────────────────────────┐    ┌──────────────────────────────┐    ┌──────────────────────────────┐    ┌─────────────────────────────┐  
│ • Create protocol.py Enums   │    │ • Write compactor.py         │    │ • Write quali\_engine.py      │    │ • Build index.html UI       │  
│ • Write struct\_unpacker.py   │ ─\> │   (Dual Ring Buffers)        │ ─\> │ • Write race\_engine.py       │ ─\> │ • Write app.js Web Speech   │  
│ • Write udp\_listener.py      │    │ • Write thermal\_filter.py    │    │ • Implement rival\_filter.py  │    │   TTS audio manager         │  
│   (Heartbeat pause check)    │    │ • Write audio\_queue.py       │    │ • Test weather & SC logic    │    │ • End-to-end integration    │  
└──────────────────────────────┘    └──────────────────────────────┘    └──────────────────────────────┘    └─────────────────────────────┘  
