---
name: RUG DESK
description: Dense, quiet paper-trading observation console
colors:
  background: "#101414"
  panel: "#151a1b"
  raised: "#1b2223"
  line: "#2b3435"
  text: "#e6edeb"
  muted: "#9baaa6"
  positive: "#8cdeb3"
  negative: "#f39796"
  warning: "#edc38b"
  secondary-series: "#a3bdf0"
typography:
  body:
    fontFamily: "Segoe UI, system-ui, sans-serif"
    fontSize: "13px"
  title:
    fontFamily: "Segoe UI, system-ui, sans-serif"
    fontSize: "25px"
    fontWeight: 550
    letterSpacing: "-0.6px"
  numeric:
    fontFamily: "Consolas, Cascadia Mono, monospace"
    fontSize: "25px"
    fontWeight: 400
rounded:
  control: "4px"
  panel: "8px"
spacing:
  panel-gap: "16px"
  panel-inset: "18px"
---
# RUG DESK design system

## Overview
An operational trading console, following the user's explicit brief. The account comes first, followed by observed prices, risk rules, position inventory and audit evidence. This is an application surface in Operate mode, with ordinary controls and no decorative motion or generated financial imagery.

## Colors
Charcoal surfaces support prolonged observation on the user's Windows desktop. Green carries positive values, selected account and price history; red means negative values or failure; amber means uncertainty and paper mode. Every status also has a text label.

## Typography
Segoe UI carries the application. Consolas is reserved for money, quantities and addresses. Account metrics use 25px; headers 25px/12px; dense data 11px; secondary annotations 9–11px. Use browser zoom where the operator prefers larger density. Financial figures use tabular numerals.

## Layout
A 205px bot rail, full-width account strip, price chart with 300px strategy panel, then tabbed ledger and evidence sections. The rail becomes horizontal below 1000px; content stacks below 760px. Data tables scroll within their panel on small screens. Main content is capped at 1850px.

## Elevation & Depth
Flat tonal surfaces with single-pixel borders. No shadows or ornamental glows.

## Shapes
8px panel corners, 4px controls, restrained small labels. SVG paths are used only for functional icons; canvas charts derive entirely from observations.

## Components
Native select controls, labeled checkboxes, keyboard-accessible tabs and a search field. Selected rows identify the chart's asset. Charts show exact recorded points and genuine observation gaps; tooltips distinguish simulated fills from market observations. Disconnected, empty, setup-blocked and missing-data states are explicit.

## Do's and Don'ts
Do preserve each account's state and timestamps. Do use text as well as color for status. Don't fabricate sample prices, volume, trades, performance or provider passes. Don't add manual Buy/Sell controls to the automatic-trading product.
