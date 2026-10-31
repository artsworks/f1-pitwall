# Team orders

## Summary

Participants data lets Pitwall identify a teammate by matching team IDs. The game does not report team orders or requests. This change adds hold-position, strategy-split, and teammate-tow calls.

## Sources and real call wording

| Event | Wording | URL |
|---|---|---|
| Australia 2009, McLaren to Hamilton under the safety car | "Hold position. Hold position." | [Autosport transcript](https://www.autosport.com/f1/news/transcript-of-mclaren-radio-transmission-4427501/4427501/) |
| Malaysia 2013, Red Bull Multi 21 and Mercedes holding Rosberg behind Hamilton | Red Bull's order was ignored by Vettel. Brawn told Rosberg to hold position. Rocquelin said, "Try to look after your tyres, you're too close in the fast corners" and "be patient, only half race yet". | [RaceFans](https://www.racefans.net/2013/03/25/red-bull-mercedes-resorting-team-orders/), [BBC Sport](https://www.bbc.com/sport/formula1/21917899) |
| Germany 2018, Ferrari to Raikkonen | Jock Clear said, "you two are on different strategies ... we'd like you not to hold up Seb". | [ESPN](https://www.espn.com/f1/story/_/id/24167353/kimi-raikkonen-ferrari-team-order-radio-exchange-full) |
| Britain 2019, Red Bull to Gasly | Red Bull told Gasly three times over a lap that Verstappen was behind "on a different strategy". Horner then ordered the swap. | [RaceFans](https://www.racefans.net/2019/07/15/im-not-holding-him-why-horner-had-to-order-gasly-to-let-verstappen-through/) |
| Hungary 2024, McLaren swap back | On lap 48: "Oscar has just pitted. He'll likely come out just behind you. We'd like to re-establish the order at your convenience". On lap 51: "once you get to Lando, we'll swap positions". Norris yielded on lap 68 of 70 after about 20 laps of requests. | [Motorsport.com](https://www.motorsport.com/f1/news/how-mclarens-radio-messages-played-out-in-the-hungarian-gp/10637343/), [PlanetF1](https://www.planetf1.com/news/mclaren-radio-messages-lando-norris-transcript-2024-hungarian-grand-prix) |
| Singapore 2023, Sainz keeping Norris in DRS range | Sainz slowed to keep Norris in DRS range against Mercedes. He asked for the gap to Norris each lap. | [Formula 1](https://www.formula1.com/en/latest/article/sainz-credits-key-carlando-moment-for-preventing-him-being-dead-meat-on-his.3EkQ4E8uKvn2kJdVgZksAE), [Motorsport.com](https://www.motorsport.com/f1/news/sainz-cheeky-singapore-f1-strategy-to-keep-norris-in-drs-range-a-risk/10521757/) |
| Monza 2019, Ferrari qualifying tow plan | The tow plan broke down. Drivers waited for a tow, and most missed the line. | [RaceFans](https://www.racefans.net/2019/09/07/analysis-drivers-team-radio-transcripts-reveal-how-f1s-qualifying-shambles-unfolded/), [Formula 1](https://www.formula1.com/en/latest/article/ferrari-did-not-stick-to-monza-qualifying-gameplan-says-vettel.4x2Av5tugoF7lX4u6rHsOH) |

Hold-position and strategy-split calls include a gap and can recur over a lap or more. Teams agree swap-back orders before a race and repeat them for many laps. Teams set qualifying tow plans before the session.

## Telemetry signals

| Need | Packet field | Status |
|---|---|---|
| Teammate identity | Participants, packet ID 4, `m_teamId`. The packet also has `m_aiControlled`, `m_myTeam`, and `m_raceNumber`. | Available. `_teammate()` already matches team IDs. The other fields are not needed. |
| Gap and order | LapData `m_carPosition` and `m_totalDistance` | Available. |
| Teammate compound and age | CarStatus `m_visualTyreCompound`; Session History tyre stints | Available. Rival age is unknown when telemetry is restricted or the current stint is uncertain after a pit stop. |
| Teammate stops | LapData `m_numPitStops` | Available. |
| Laps left | Session `m_totalLaps` and LapData `m_currentLapNum` | Available. |
| Qualifying tow | LapData distances and `m_driverStatus` | Available. These fields locate traffic and show whether a car is on a flying lap. |
| Team order or swap request | No packet field | Unavailable. The game does not report an order, a request, or acceptance. |
| Teammate pit plan or target lap | No packet field | Unavailable. |
| Teammate actions | No packet field | Unavailable. The AI teammate cannot follow instructions to hold, yield, or tow. |
| 2026 overtake mode | No packet field | Unavailable. DRS is removed, overtake mode is automatic, and the game does not report whether a teammate uses it to defend. |

## Calls

### Implemented

`teammate_hold` fires in a race when a teammate is fighting directly ahead and one to three laps remain. The `teammate_hold_laps` threshold sets the window to three laps. The call includes the gap and laps remaining.

`teammate_strategy_split` fires in a race when both cars are running, both compounds and ages are known, and both stints are at least five laps old. It reports a split when the compound, stop count, or tyre-age difference meets the threshold. The five-lap minimum suppresses a normal one-lap pit-cycle offset.

`prep_tow_teammate` fires in practice or qualifying on an out-lap. A teammate must be on a flying lap, within the tow gap of 0.7 to 2.0 seconds, and the player must be within the preparation distance.

### Not implemented

Pitwall cannot identify a received hold-position order or a swap-back request. The packets have no order field, and the AI teammate cannot act on either request.

Pitwall does not ask a teammate to help with a DRS train or a coordinated qualifying tow. The game exposes no team instruction channel.

The existing `teammate_fight` call reports a close teammate fight. `double_stack` reports a shared pit entry. `contact_check_teammate` identifies a teammate in a collision.

## Phrasing

| Rule | Variants | Source wording followed |
|---|---|---|
| `teammate_hold` | "Hold position behind {teammate_name}. Gap {teammate_gap_s:.1f}, {laps_remaining} to go"; "{teammate_name} ahead, {teammate_gap_s:.1f}. Hold position, {laps_remaining} to go"; "Hold position. {laps_remaining} to go, {teammate_name} {teammate_gap_s:.1f} ahead"; "{laps_remaining} to go. Hold behind {teammate_name}, gap {teammate_gap_s:.1f}" | Australia 2009 hold-position wording. The added gap and lap count follow radio examples that repeat the order with race context. |
| `teammate_strategy_split` | "{teammate_name} is on a different strategy. On {teammate_compound_name}s, {teammate_tyre_age} laps old"; "Different strategy for {teammate_name}, {teammate_tyre_age} laps on {teammate_compound_name}s"; "{teammate_name} on {teammate_compound_name}s, {teammate_tyre_age} laps old. Different plan to you"; "Your team-mate is on another strategy, {teammate_compound_name}s {teammate_tyre_age} laps old" | Germany 2018 and Britain 2019 different-strategy messages. The tyre compound and age add measurable context. |
| `prep_tow_teammate` | "{traffic_ahead_name} {traffic_ahead_s:.1f} ahead. Use his tow."; "Team-mate {traffic_ahead_s:.1f} ahead. Take the tow."; "Tow from {traffic_ahead_name}, {traffic_ahead_s:.1f} ahead. Stay in it."; "{traffic_ahead_s:.1f} to {traffic_ahead_name}. Good gap for the tow." | Monza 2019 qualifying tow planning. The call gives the teammate name and gap. |

## Scenario

`scenarios/brazil-team-orders.yaml` has status `target`. It expects no strategy-split call for the one-lap pit offset and no hold call before the last three laps. Run the bench on a machine with the Brazil recording.
