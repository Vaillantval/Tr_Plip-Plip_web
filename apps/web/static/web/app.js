// Plip-Plip, site client : tout le JavaScript des pages.
//
// Aucun script ni gestionnaire (onclick, onsubmit...) n'est ecrit dans les
// gabarits : la politique de securite (CSP, apps/web/middleware.py) n'autorise
// que les fichiers servis par le site. Chaque comportement s'accroche a un
// attribut data-* et marche sans JavaScript, en moins confortable.
(function () {
  "use strict";

  // Bouton « Afficher » des champs mot de passe (partials/password_field.html).
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-reveal]");
    if (!button) return;
    var input = document.getElementById(button.getAttribute("data-reveal"));
    if (!input) return;
    var show = input.type === "password";
    input.type = show ? "text" : "password";
    button.textContent = button.getAttribute(show ? "data-hide" : "data-show");
    button.setAttribute("aria-pressed", show ? "true" : "false");
  });

  // Formulaire envoye : roue d'attente sur le bouton principal (app.css), et
  // bouton desactive sur les formulaires data-once (pas de double envoi).
  document.addEventListener("submit", function (event) {
    if (event.defaultPrevented) return;
    var form = event.target;
    var button = event.submitter || form.querySelector("button[type=submit].block");
    if (button && button.classList.contains("block")) button.classList.add("is-loading");
    if (form.hasAttribute("data-once")) {
      form.querySelectorAll("button[type=submit]").forEach(function (b) { b.disabled = true; });
    }
  });

  // Retour arriere : la page revient du cache, on rend les boutons.
  window.addEventListener("pageshow", function (event) {
    if (!event.persisted) return;
    document.querySelectorAll("button.is-loading, form[data-once] button[type=submit]").forEach(function (button) {
      button.classList.remove("is-loading");
      button.disabled = false;
    });
  });

  // Menus qui s'appliquent des qu'ils changent (langue du pied de page).
  document.addEventListener("change", function (event) {
    var select = event.target.closest("select[data-autosubmit]");
    if (select && select.form) select.form.requestSubmit();
  });

  // Accueil : jamais le meme portefeuille des deux cotes. Si l'envoi rejoint
  // la reception, la reception passe sur le premier autre portefeuille. Pas
  // de nouvel evenement : le « change » de l'envoi remonte ensuite au
  // formulaire, et le devis part avec la bonne paire.
  document.addEventListener("change", function (event) {
    if (event.target.id !== "id_source_wallet") return;
    var source = event.target;
    var destination = document.getElementById("id_destination_wallet");
    if (!destination || destination.value !== source.value) return;
    for (var i = 0; i < destination.options.length; i++) {
      if (destination.options[i].value !== source.value) {
        destination.value = destination.options[i].value;
        return;
      }
    }
  }, true);
})();
