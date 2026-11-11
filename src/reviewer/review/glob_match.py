import re


def glob_to_regex(glob: str) -> re.Pattern[str]:
    """Converts a simple glob ("docs/**", "**/*.generated.ts", "src/*.js") into an anchored regex."""
    out: list[str] = []
    i = 0
    while i < len(glob):
        c = glob[i]
        if c == "*":
            if glob[i + 1 : i + 2] == "*":
                out.append(".*")
                i += 1
                if glob[i + 1 : i + 2] == "/":
                    i += 1  # "**/" also matches zero directories
            else:
                out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile(f"^{''.join(out)}$")
