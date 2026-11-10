// A terminal that replays a capture made by media/capture.py.
//
//   const terminal = createTerminal(element, window.CAPTURES.demo, { start: 0.5 });
//   terminal.seek(t);          // draw the terminal as it looked t seconds in
//   terminal.end               // when the command's output has finished
//   terminal.cues()            // when each key, Enter and output line happens, for the soundtrack
//
// Everything is computed from t, so a frame looks the same however it is reached.
export function createTerminal(element, capture, options = {}) {
  const start = options.start ?? 0.5;            // when typing begins
  const typeSpeed = options.typeSpeed ?? 0.065;  // seconds per character
  const pause = options.pause ?? 0.35;           // between the last key and Enter
  const speed = options.speed ?? 1;              // output playback speed
  const lead = options.lead ?? 0.35;             // startup delay shown before the first line
  const prompt = options.prompt ?? "$";

  const lineHeight = parseFloat(getComputedStyle(element).getPropertyValue("--term-line")) || 34;
  const linesElement = document.createElement("div");
  linesElement.className = "lines";
  element.appendChild(linesElement);

  const command = capture.command;
  const typedAt = start + command.length * typeSpeed;
  const enterAt = typedAt + pause;
  const first = capture.lines.length ? capture.lines[0].t : 0;
  const at = (line) => enterAt + lead + (line.t - first) / speed;
  const output = capture.lines.filter((line) => !line.status).map((line) => ({ html: line.html, at: at(line) }));
  const status = capture.lines.filter((line) => line.status).map((line) => ({ html: line.html, at: at(line) }));
  // The spinner line goes when the run finishes, or as soon as the next output line arrives.
  const lastStatus = status.length ? status[status.length - 1].at : 0;
  const nextOutput = output.find((line) => line.at > lastStatus);
  const statusEnds = status.length ? Math.min(lastStatus + 0.09, nextOutput ? nextOutput.at : Infinity) : 0;
  const end = (output.length ? output[output.length - 1].at : enterAt) + 0.3;

  function row(html) {
    const line = document.createElement("div");
    line.className = "line";
    line.innerHTML = html || "&nbsp;";
    return line;
  }

  function seek(t) {
    const typed = t < start ? 0 : Math.min(command.length, Math.floor((t - start) / typeSpeed) + 1);
    const blink = Math.floor(t * 2) % 2 === 0;
    const cursor = (visible) => (visible ? '<span class="cursor"></span>' : "");
    const rows = [];
    rows.push(
      `<span class="prompt">${prompt}</span> <span class="bold">${command.slice(0, typed)}</span>` +
        cursor(t < enterAt && (typed < command.length || blink)),
    );
    for (const line of output) if (line.at <= t) rows.push(line.html);
    if (t < statusEnds) {
      const current = status.filter((line) => line.at <= t).pop();
      if (current) rows.push(current.html);
    }
    if (t >= end) rows.push(`<span class="prompt">${prompt}</span> ${cursor(blink)}`);

    linesElement.replaceChildren(...rows.map(row));
    const overflow = rows.length * lineHeight + 44 - element.clientHeight;
    linesElement.style.transform = `translateY(${-Math.max(0, overflow)}px)`;
  }

  const plain = (html) => html.replace(/<[^>]+>/g, "").replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").trim();

  function cues() {
    return [
      ...[...command].map((_, index) => ({ t: start + index * typeSpeed, kind: "key" })),
      { t: enterAt, kind: "enter" },
      ...output.filter((line) => plain(line.html)).map((line) => ({ t: line.at, kind: "line", text: plain(line.html) })),
    ];
  }

  return { seek, end, enterAt, cues };
}
