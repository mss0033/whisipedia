import './styles/base.css';
import './styles/themes.css';
import './styles/pages.css';
import { App } from './app.ts';

const root = document.getElementById('app');
if (root) {
  const app = new App(root);
  app.start();
}
