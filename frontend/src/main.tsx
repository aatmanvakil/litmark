import { render } from 'preact'

import { App } from './App'
import './styles.css'
// Bundled with the package; nothing is fetched from a CDN at runtime.
import 'katex/dist/katex.min.css'

const root = document.getElementById('app')
if (root) render(<App />, root)
