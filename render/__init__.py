"""View-only pygame renderer for watching agents play the faithful sim.

``render.play`` drives the *unmodified* :class:`sim.env.KitchenSim` with either the
greedy :class:`agents.planner.Planner` or a trained MaskablePPO policy and draws
every tick so the survival wall (death at the 3-strike limit) can be diagnosed by
eye.  Nothing here changes sim mechanics or agent decision logic — it only reads
state and renders it (including the agent-hidden ground truth: VIP orders and
eating-customer stand occupancy).
"""
