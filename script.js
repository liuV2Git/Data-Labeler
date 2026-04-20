const greetings = [
  "Hello, world!",
  "Hello from Data Labeler!",
  "Hello, Codex teammate!",
  "Hello, Toronto time!"
];

const message = document.querySelector("#message");
const button = document.querySelector("#toggle-button");

let index = 0;

button.addEventListener("click", () => {
  index = (index + 1) % greetings.length;
  message.textContent = greetings[index];
});
