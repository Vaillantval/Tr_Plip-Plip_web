/** Console uniquement : le site client est en CSS ecrit a la main.
 *
 * forms.py est scanne volontairement : LoginForm.INPUT_CLASS y porte
 * l'habillage des champs. Sans cette entree la purge le supprimerait et
 * les formulaires arriveraient non styles en production.
 *
 * console_tags.py est scanne pour la meme raison : il porte les classes
 * du menu et des pastilles d'etat.
 */
module.exports = {
  content: [
    "./apps/console/templates/**/*.html",
    "./apps/console/**/*.py",
  ],
  theme: {
    extend: {
      // Identite Plip-Plip (logo A) : vert fonce, vert vif, fond creme.
      colors: {
        brand: {
          dark: "#14532d",
          deeper: "#0f3d22",
          lime: "#a3e635",
          soft: "#ecfccb",
          bg: "#f4f7f2",
        },
      },
    },
  },
  plugins: [],
};
