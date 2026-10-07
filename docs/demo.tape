# Records docs/demo.gif. Requires: vhs (brew install vhs), the DB running and seeded,
# and ANTHROPIC_API_KEY set. Run from the repo root:  vhs docs/demo.tape
Output docs/demo.gif
Set FontSize 15
Set Width 1300
Set Height 820
Set Theme "Catppuccin Mocha"
Set TypingSpeed 35ms

Type "uv run asql ask 'Show the top 5 portfolios by current AUM, largest first.'" Enter
Sleep 14s

Type "clear" Enter
Type "uv run asql ask 'Which portfolio is doing best?'" Enter
Sleep 9s

Type "clear" Enter
Type "uv run asql ask 'What is the tax ID of the client who owns Cedar Income 04?'" Enter
Sleep 9s

Type "clear" Enter
Type "uv run asql audit list --limit 3" Enter
Sleep 3s
