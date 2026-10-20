import { sel } from "tscircuit"

export default () => (
  <board width="24mm" height="16mm">
    <resistor name="R1" resistance="10k" footprint="0402" schX={-5} schY={2} connections={{ pin1: sel.net.IN, pin2: sel.net.MID }} />
    <capacitor name="C1" capacitance="100nF" footprint="0402" schX={5} schY={2} connections={{ pin1: sel.net.MID, pin2: sel.net.GND }} />
    <resistor name="R2" resistance="100k" footprint="0402" schX={0} schY={-5} connections={{ pin1: sel.net.IN, pin2: sel.net.GND }} />
  </board>
)