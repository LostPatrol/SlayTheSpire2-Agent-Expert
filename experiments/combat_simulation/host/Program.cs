// Persistent JSONL entrypoint for the isolated managed combat experiment.
using System.Text;
using System.Text.Json;
using CombatSimulation;

Console.InputEncoding = Encoding.UTF8;
Console.OutputEncoding = new UTF8Encoding(false);
var options = new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower };
string? line;
while ((line = Console.ReadLine()) is not null)
{
    try
    {
        using var document = JsonDocument.Parse(line);
        var request = document.RootElement;
        string op = request.TryGetProperty("op", out var operation) ? operation.GetString() ?? "inspect" : "inspect";
        int seed = request.TryGetProperty("seed", out var seedValue) ? seedValue.GetInt32() : 0;
        var position = SimulationPosition.Import(request.GetProperty("public_state"), seed);
        object response;
        if (op == "evaluate") response = BoundedEvaluation.Run(request);
        else if (op == "search") response = Search.Run(position, request);
        else if (op is "inspect" or "simulate")
        {
            var events = new List<object>();
            var errors = new List<object>();
            if (op == "simulate" && request.TryGetProperty("actions", out var actions))
            {
                int actionNumber = 0;
                foreach (var action in actions.EnumerateArray())
                {
                    var parsed = BoundedEvaluation.ParseAction(action);
                    if (!position.Apply(parsed, out var error))
                    {
                        errors.Add(new { action_index = actionNumber, error });
                        break;
                    }
                    events.Add(new { action_index = actionNumber++, action = parsed, applied = true });
                }
            }
            var legalActions = position.LegalActions();
            var legalPotions = position.PotionActions();
            response = new
            {
                ok = errors.Count == 0 && position.Support.Level != "unsupported", support = position.Support,
                state = position.Snapshot(), legal_actions = legalActions, legal_potion_actions = legalPotions, events, errors,
                score_support = position.ScoreSupport,
                scope = "supported_deterministic_current_player_turn_actions_only",
            };
        }
        else response = new { ok = false, errors = new[] { "unknown_op" } };
        Console.WriteLine(JsonSerializer.Serialize(response, options));
    }
    catch (Exception error)
    {
        Console.WriteLine(JsonSerializer.Serialize(new { ok = false, errors = new[] { error.GetType().Name + ": " + error.Message } }, options));
    }
    Console.Out.Flush();
}
