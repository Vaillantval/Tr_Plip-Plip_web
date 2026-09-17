/** Console uniquement : le site client est en CSS ecrit a la main.
 *
 * forms.py est scanne volontairement : LoginForm.INPUT_CLASS y porte
 * l'habillage des champs. Sans cette entree la purge le supprimerait et
 * les formulaires arriveraient non styles en production.
 */
module.exports = {
  content: [
    "./apps/console/templates/**/*.html",
    "./apps/console/**/*.py",
  ],
  theme: { extend: {} },
  plugins: [],
};
